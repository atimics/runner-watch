use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const ARTIFACT_SCHEMA: &str = "stonks.integer_ranker.v1";
pub const FEATURE_SCALE: i64 = 1_000;
pub const NORMALIZED_SCALE: i64 = 1_024;
pub const WEIGHT_SCALE: i64 = 1_048_576;
pub const PROBABILITY_SCALE: i64 = 1_000_000;
pub const TEMPERATURE_SCALE: i64 = 1_000;
pub const RETURN_SCALE: i64 = 100;

const EXP_SCALE: i64 = 1_048_576;
const LN_2_EXP_SCALE: i64 = 726_817;
const LN_2_MICROS: i64 = 693_147;
const MAX_LOGIT: i64 = 32 * WEIGHT_SCALE;
const MAX_NORMALIZED_FEATURE: i64 = 16 * NORMALIZED_SCALE;
const LEARNING_RATE_MICROS: i64 = 60_000;
const L2_MICROS: i64 = 3_000;
const VALIDATION_CHECK_INTERVAL: usize = 10;
const EARLY_STOPPING_PATIENCE: usize = 8;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct IntegerArtifact {
    pub schema: String,
    pub feature_names: Vec<String>,
    pub feature_scale: i64,
    pub normalized_scale: i64,
    pub weight_scale: i64,
    pub probability_scale: i64,
    pub temperature_scale: i64,
    pub return_scale: i64,
    pub means: Vec<i64>,
    pub scales: Vec<i64>,
    pub weights: Vec<Vec<i64>>,
    pub bias: Vec<i64>,
    pub temperature_milli: i64,
    pub timeout_return_bp: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TrainingRow {
    pub ticker: String,
    pub features: Vec<i64>,
    pub outcome: usize,
    pub outcome_return_bp: i64,
    pub baseline_score_milli: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PredictionRow {
    pub id: String,
    pub ticker: String,
    pub features: Vec<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Prediction {
    pub id: String,
    pub ticker: String,
    pub rank: usize,
    pub probability_down_ppm: i64,
    pub probability_timeout_ppm: i64,
    pub probability_up_ppm: i64,
    pub expected_return_bp: i64,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum Request {
    Train {
        feature_names: Vec<String>,
        groups: Vec<Vec<TrainingRow>>,
        epochs: usize,
    },
    Predict {
        artifact: IntegerArtifact,
        rows: Vec<PredictionRow>,
    },
}

#[derive(Debug, Clone)]
struct NormalizedTrainingRow {
    ticker: String,
    features: Vec<i64>,
    outcome: usize,
    outcome_return_bp: i64,
    baseline_score_milli: i64,
}

#[derive(Debug, Clone, Serialize)]
struct TrainingControl {
    requested_epochs: usize,
    trained_epochs: usize,
    best_epoch: usize,
    validation_checks: usize,
    validation_log_loss_micros: i64,
    stopped_early: bool,
}

pub fn execute(request: Request) -> Result<Value, String> {
    match request {
        Request::Train {
            feature_names,
            groups,
            epochs,
        } => train(feature_names, groups, epochs),
        Request::Predict { artifact, rows } => {
            let predictions = predict(&artifact, rows)?;
            Ok(json!({"ok": true, "predictions": predictions}))
        }
    }
}

fn train(
    feature_names: Vec<String>,
    groups: Vec<Vec<TrainingRow>>,
    epochs: usize,
) -> Result<Value, String> {
    if feature_names.is_empty() {
        return Err("feature_names cannot be empty".into());
    }
    if groups.len() < 3 {
        return Err("at least three complete groups are required".into());
    }
    validate_training_rows(&feature_names, &groups)?;

    let holdout_count = ((groups.len() + 2) / 5).clamp(2, groups.len() - 1);
    let validation_count = holdout_count / 2;
    let train_end = groups.len() - holdout_count;
    let validation_end = train_end + validation_count;
    let train_groups = &groups[..train_end];
    let validation_groups = &groups[train_end..validation_end];
    let test_groups = &groups[validation_end..];
    let (means, scales) = normalizer(train_groups, feature_names.len());
    let normalized_train = normalize_training_groups(train_groups, &means, &scales);
    let normalized_validation = normalize_training_groups(validation_groups, &means, &scales);
    let normalized_test = normalize_training_groups(test_groups, &means, &scales);

    let mut artifact = IntegerArtifact {
        schema: ARTIFACT_SCHEMA.into(),
        feature_names,
        feature_scale: FEATURE_SCALE,
        normalized_scale: NORMALIZED_SCALE,
        weight_scale: WEIGHT_SCALE,
        probability_scale: PROBABILITY_SCALE,
        temperature_scale: TEMPERATURE_SCALE,
        return_scale: RETURN_SCALE,
        means,
        scales,
        weights: vec![vec![0; 3]; normalized_train[0][0].features.len()],
        bias: vec![0; 3],
        temperature_milli: TEMPERATURE_SCALE,
        timeout_return_bp: median_timeout_return(&normalized_train),
    };

    let training_control = fit(
        &mut artifact,
        &normalized_train,
        &normalized_validation,
        epochs.max(1),
    );
    artifact.temperature_milli = calibrate_temperature(&artifact, &normalized_validation);

    let train_metrics = evaluate(&artifact, &normalized_train);
    let validation_metrics = evaluate(&artifact, &normalized_validation);
    let test_metrics = evaluate(&artifact, &normalized_test);
    let metrics = json!({
        "numeric_contract": {
            "features": "thousandths",
            "probabilities": "parts_per_million",
            "returns": "basis_points",
            "integer_only": true,
        },
        "train": train_metrics,
        "validation": validation_metrics,
        "test": test_metrics,
        "feature_names": artifact.feature_names,
        "target": "hit_plus_8_before_minus_4_within_60_minutes",
        "class_names": ["down", "timeout", "up"],
        "training_control": training_control,
        "temperature_milli": artifact.temperature_milli,
        "timeout_return_bp": artifact.timeout_return_bp,
        "split": "oldest_80_percent_train_next_10_percent_validation_newest_10_percent_test",
    });
    Ok(json!({"ok": true, "artifact": artifact, "metrics": metrics}))
}

fn validate_training_rows(
    feature_names: &[String],
    groups: &[Vec<TrainingRow>],
) -> Result<(), String> {
    for (group_index, group) in groups.iter().enumerate() {
        if group.len() < 2 {
            return Err(format!("group {group_index} has fewer than two candidates"));
        }
        for row in group {
            if row.features.len() != feature_names.len() {
                return Err(format!(
                    "{} has {} features; expected {}",
                    row.ticker,
                    row.features.len(),
                    feature_names.len()
                ));
            }
            if row.outcome >= 3 {
                return Err(format!("{} has an invalid outcome", row.ticker));
            }
        }
    }
    Ok(())
}

fn normalizer(groups: &[Vec<TrainingRow>], feature_count: usize) -> (Vec<i64>, Vec<i64>) {
    let row_count: i128 = groups.iter().map(|group| group.len() as i128).sum();
    let mut sums = vec![0_i128; feature_count];
    for row in groups.iter().flatten() {
        for (index, value) in row.features.iter().enumerate() {
            sums[index] += i128::from(*value);
        }
    }
    let means: Vec<i64> = sums
        .into_iter()
        .map(|sum| rounded_div(sum, row_count) as i64)
        .collect();
    let mut squared = vec![0_u128; feature_count];
    for row in groups.iter().flatten() {
        for (index, value) in row.features.iter().enumerate() {
            let delta = i128::from(*value) - i128::from(means[index]);
            squared[index] += (delta * delta) as u128;
        }
    }
    let scales = squared
        .into_iter()
        .map(|sum| integer_sqrt(sum / row_count as u128).max(1) as i64)
        .collect();
    (means, scales)
}

fn normalize_training_groups(
    groups: &[Vec<TrainingRow>],
    means: &[i64],
    scales: &[i64],
) -> Vec<Vec<NormalizedTrainingRow>> {
    groups
        .iter()
        .map(|group| {
            group
                .iter()
                .map(|row| NormalizedTrainingRow {
                    ticker: row.ticker.clone(),
                    features: normalize_features(&row.features, means, scales),
                    outcome: row.outcome,
                    outcome_return_bp: row.outcome_return_bp,
                    baseline_score_milli: row.baseline_score_milli,
                })
                .collect()
        })
        .collect()
}

fn normalize_features(features: &[i64], means: &[i64], scales: &[i64]) -> Vec<i64> {
    features
        .iter()
        .enumerate()
        .map(|(index, value)| {
            let numerator = i128::from(*value - means[index]) * i128::from(NORMALIZED_SCALE);
            let normalized = rounded_div(numerator, i128::from(scales[index])) as i64;
            normalized.clamp(-MAX_NORMALIZED_FEATURE, MAX_NORMALIZED_FEATURE)
        })
        .collect()
}

fn fit(
    artifact: &mut IntegerArtifact,
    groups: &[Vec<NormalizedTrainingRow>],
    validation_groups: &[Vec<NormalizedTrainingRow>],
    epochs: usize,
) -> TrainingControl {
    let requested_epochs = epochs.max(1);
    let mut best_artifact = artifact.clone();
    let mut best_epoch = 0;
    let mut best_loss = multiclass_log_loss(artifact, validation_groups, TEMPERATURE_SCALE);
    let mut trained_epochs = 0;
    let mut validation_checks = 0;
    let mut checks_without_improvement = 0;

    for epoch in 0..requested_epochs {
        fit_epoch(artifact, groups, epoch);
        trained_epochs = epoch + 1;
        if trained_epochs % VALIDATION_CHECK_INTERVAL != 0 && trained_epochs != requested_epochs {
            continue;
        }
        validation_checks += 1;
        let loss = multiclass_log_loss(artifact, validation_groups, TEMPERATURE_SCALE);
        if loss < best_loss {
            best_loss = loss;
            best_epoch = trained_epochs;
            best_artifact = artifact.clone();
            checks_without_improvement = 0;
        } else {
            checks_without_improvement += 1;
        }
        if checks_without_improvement >= EARLY_STOPPING_PATIENCE {
            break;
        }
    }

    *artifact = best_artifact;
    TrainingControl {
        requested_epochs,
        trained_epochs,
        best_epoch,
        validation_checks,
        validation_log_loss_micros: best_loss,
        stopped_early: trained_epochs < requested_epochs,
    }
}

fn fit_epoch(artifact: &mut IntegerArtifact, groups: &[Vec<NormalizedTrainingRow>], epoch: usize) {
    let feature_count = artifact.weights.len();
    let group_count = groups.len() as i128;
    let mut weight_gradient = vec![[0_i128; 3]; feature_count];
    let mut bias_gradient = [0_i128; 3];
    for group in groups {
        let mut group_weight_gradient = vec![[0_i128; 3]; feature_count];
        let mut group_bias_gradient = [0_i128; 3];
        for row in group {
            let probabilities = probabilities(artifact, &row.features, TEMPERATURE_SCALE);
            for class in 0..3 {
                let target = if row.outcome == class {
                    PROBABILITY_SCALE
                } else {
                    0
                };
                let difference = i128::from(probabilities[class] - target);
                group_bias_gradient[class] += difference;
                for (feature_index, feature) in row.features.iter().enumerate() {
                    group_weight_gradient[feature_index][class] +=
                        i128::from(*feature) * difference;
                }
            }
        }
        let group_size = group.len() as i128;
        for class in 0..3 {
            bias_gradient[class] += group_bias_gradient[class] / group_size;
        }
        for feature_index in 0..feature_count {
            for class in 0..3 {
                weight_gradient[feature_index][class] +=
                    group_weight_gradient[feature_index][class] / group_size;
            }
        }
    }

    let step_micros = i128::from(LEARNING_RATE_MICROS) * 250 / (250 + epoch as i128);
    let weight_denominator = i128::from(1_000_000)
        * group_count
        * i128::from(NORMALIZED_SCALE)
        * i128::from(PROBABILITY_SCALE);
    let bias_denominator = i128::from(1_000_000) * group_count * i128::from(PROBABILITY_SCALE);
    let weight_clip =
        10 * group_count * i128::from(NORMALIZED_SCALE) * i128::from(PROBABILITY_SCALE);
    let bias_clip = 10 * group_count * i128::from(PROBABILITY_SCALE);

    for (weight_row, gradient_row) in artifact.weights.iter_mut().zip(&weight_gradient) {
        for (weight, raw_gradient) in weight_row.iter_mut().zip(gradient_row) {
            let gradient = (*raw_gradient).clamp(-weight_clip, weight_clip);
            let update = rounded_div(
                step_micros * i128::from(WEIGHT_SCALE) * gradient,
                weight_denominator,
            );
            let l2_update = rounded_div(
                step_micros * i128::from(L2_MICROS) * i128::from(*weight),
                1_000_000_i128 * 1_000_000_i128,
            );
            *weight = (i128::from(*weight) - update - l2_update)
                .clamp(i128::from(-MAX_LOGIT), i128::from(MAX_LOGIT)) as i64;
        }
    }
    for (bias, raw_gradient) in artifact.bias.iter_mut().zip(&bias_gradient) {
        let gradient = (*raw_gradient).clamp(-bias_clip, bias_clip);
        let update = rounded_div(
            step_micros * i128::from(WEIGHT_SCALE) * gradient,
            bias_denominator,
        );
        *bias = (i128::from(*bias) - update).clamp(i128::from(-MAX_LOGIT), i128::from(MAX_LOGIT))
            as i64;
    }
}

fn logits(artifact: &IntegerArtifact, features: &[i64]) -> [i64; 3] {
    let mut output = [0_i64; 3];
    for (class, output_value) in output.iter_mut().enumerate() {
        let mut value = i128::from(artifact.bias[class]);
        for (feature_index, feature) in features.iter().enumerate() {
            value += i128::from(*feature) * i128::from(artifact.weights[feature_index][class])
                / i128::from(NORMALIZED_SCALE);
        }
        *output_value = value.clamp(i128::from(-MAX_LOGIT), i128::from(MAX_LOGIT)) as i64;
    }
    output
}

fn probabilities(artifact: &IntegerArtifact, features: &[i64], temperature_milli: i64) -> [i64; 3] {
    let raw = logits(artifact, features);
    let maximum = *raw.iter().max().unwrap_or(&0);
    let temperature = temperature_milli.max(1);
    let mut weights = [0_i64; 3];
    for class in 0..3 {
        let difference = i128::from(raw[class] - maximum) * i128::from(TEMPERATURE_SCALE)
            / i128::from(temperature);
        weights[class] = exp_negative(difference as i64);
    }
    let denominator: i64 = weights.iter().sum::<i64>().max(1);
    let mut output = [0_i64; 3];
    for class in 0..3 {
        output[class] = (i128::from(weights[class]) * i128::from(PROBABILITY_SCALE)
            / i128::from(denominator)) as i64;
    }
    let assigned: i64 = output.iter().sum();
    let maximum_class = raw
        .iter()
        .enumerate()
        .max_by_key(|(_, value)| *value)
        .map(|(index, _)| index)
        .unwrap_or(0);
    output[maximum_class] += PROBABILITY_SCALE - assigned;
    output
}

fn exp_negative(value: i64) -> i64 {
    let clamped = value.clamp(-16 * WEIGHT_SCALE, 0);
    let magnitude = -clamped;
    let shifts = magnitude / LN_2_EXP_SCALE;
    let remainder = magnitude % LN_2_EXP_SCALE;
    let mut term = i128::from(EXP_SCALE);
    let mut sum = term;
    for divisor in 1..=12_i128 {
        term = -term * i128::from(remainder) / (i128::from(EXP_SCALE) * divisor);
        sum += term;
    }
    let shifted = if shifts >= 62 {
        0
    } else {
        (sum.max(0) as i64) >> shifts
    };
    shifted.max(1)
}

fn calibrate_temperature(artifact: &IntegerArtifact, groups: &[Vec<NormalizedTrainingRow>]) -> i64 {
    let mut best_temperature = TEMPERATURE_SCALE;
    let mut best_loss = i128::MAX;
    for temperature in (500..=3_000).step_by(50) {
        let mut loss = 0_i128;
        for row in groups.iter().flatten() {
            let probability = probabilities(artifact, &row.features, temperature)[row.outcome];
            loss += i128::from(negative_log_micros(probability));
        }
        if loss < best_loss {
            best_loss = loss;
            best_temperature = temperature;
        }
    }
    best_temperature
}

fn negative_log_micros(probability: i64) -> i64 {
    let mut scaled = probability.clamp(1, PROBABILITY_SCALE);
    let mut powers = 0_i64;
    while scaled < PROBABILITY_SCALE / 2 {
        scaled *= 2;
        powers += 1;
    }
    const LOG_SCALE: i128 = 1_000_000_000;
    let numerator = i128::from(scaled - PROBABILITY_SCALE) * LOG_SCALE;
    let denominator = i128::from(scaled + PROBABILITY_SCALE);
    let z = numerator / denominator;
    let z_squared = z * z / LOG_SCALE;
    let mut power = z;
    let mut series = 0_i128;
    for odd in (1..=19_i128).step_by(2) {
        series += power / odd;
        power = power * z_squared / LOG_SCALE;
    }
    let log_scaled_micros = 2 * series / 1_000;
    powers * LN_2_MICROS - log_scaled_micros as i64
}

fn multiclass_log_loss(
    artifact: &IntegerArtifact,
    groups: &[Vec<NormalizedTrainingRow>],
    temperature_milli: i64,
) -> i64 {
    let mut loss = 0_i128;
    let mut rows = 0_i128;
    for row in groups.iter().flatten() {
        let probability = probabilities(artifact, &row.features, temperature_milli)[row.outcome];
        loss += i128::from(negative_log_micros(probability));
        rows += 1;
    }
    rounded_div(loss, rows.max(1)) as i64
}

fn expected_return_bp(probabilities: [i64; 3], timeout_return_bp: i64) -> i64 {
    rounded_div(
        i128::from(-400) * i128::from(probabilities[0])
            + i128::from(timeout_return_bp) * i128::from(probabilities[1])
            + i128::from(800) * i128::from(probabilities[2]),
        i128::from(PROBABILITY_SCALE),
    ) as i64
}

fn evaluate(artifact: &IntegerArtifact, groups: &[Vec<NormalizedTrainingRow>]) -> Value {
    let mut selected_wins = 0_i64;
    let mut baseline_wins = 0_i64;
    let mut top5_wins = 0_i64;
    let mut baseline_top5_wins = 0_i64;
    let mut top5_rows = 0_i64;
    let mut selected_returns = 0_i128;
    let mut baseline_returns = 0_i128;
    let mut selected_return_count = 0_i64;
    let mut probability_rows: Vec<([i64; 3], usize)> = Vec::new();

    for group in groups {
        let mut scored: Vec<(usize, i64)> = group
            .iter()
            .enumerate()
            .map(|(index, row)| {
                let probability =
                    probabilities(artifact, &row.features, artifact.temperature_milli);
                probability_rows.push((probability, row.outcome));
                (
                    index,
                    expected_return_bp(probability, artifact.timeout_return_bp),
                )
            })
            .collect();
        scored.sort_by(|left, right| {
            right
                .1
                .cmp(&left.1)
                .then_with(|| group[left.0].ticker.cmp(&group[right.0].ticker))
        });
        let selected = scored[0].0;
        let baseline = group
            .iter()
            .enumerate()
            .max_by_key(|(_, row)| row.baseline_score_milli)
            .map(|(index, _)| index)
            .unwrap_or(0);
        selected_wins += i64::from(group[selected].outcome == 2);
        baseline_wins += i64::from(group[baseline].outcome == 2);
        selected_returns += i128::from(group[selected].outcome_return_bp);
        baseline_returns += i128::from(group[baseline].outcome_return_bp);
        selected_return_count += 1;

        let top_count = group.len().min(5);
        top5_wins += scored[..top_count]
            .iter()
            .map(|(index, _)| i64::from(group[*index].outcome == 2))
            .sum::<i64>();
        let mut baseline_order: Vec<usize> = (0..group.len()).collect();
        baseline_order.sort_by(|left, right| {
            group[*right]
                .baseline_score_milli
                .cmp(&group[*left].baseline_score_milli)
                .then_with(|| group[*left].ticker.cmp(&group[*right].ticker))
        });
        baseline_top5_wins += baseline_order[..top_count]
            .iter()
            .map(|index| i64::from(group[*index].outcome == 2))
            .sum::<i64>();
        top5_rows += top_count as i64;
    }

    let row_count = probability_rows.len().max(1) as i128;
    let mut log_loss = 0_i128;
    let mut brier = [0_i128; 3];
    let mut bins = [[(0_i64, 0_i128, 0_i64); 10]; 3];
    for (probability, outcome) in probability_rows {
        log_loss += i128::from(negative_log_micros(probability[outcome]));
        for class_index in 0..3 {
            let target = if outcome == class_index {
                PROBABILITY_SCALE
            } else {
                0
            };
            let difference = i128::from(probability[class_index] - target);
            brier[class_index] += difference * difference;
            let bin = ((probability[class_index] * 10 / PROBABILITY_SCALE).clamp(0, 9)) as usize;
            bins[class_index][bin].0 += 1;
            bins[class_index][bin].1 += i128::from(probability[class_index]);
            bins[class_index][bin].2 += i64::from(outcome == class_index);
        }
    }
    let ece_numerator = bins.map(|class_bins| {
        class_bins
            .iter()
            .map(|(_, probability_sum, actual_count)| {
                (*probability_sum - i128::from(*actual_count) * i128::from(PROBABILITY_SCALE)).abs()
            })
            .sum::<i128>()
    });
    let group_count = groups.len().max(1) as i128;
    json!({
        "groups": groups.len(),
        "rows": row_count,
        "selected_up_rate_ppm": selected_wins as i128 * i128::from(PROBABILITY_SCALE) / group_count,
        "baseline_selected_up_rate_ppm": baseline_wins as i128 * i128::from(PROBABILITY_SCALE) / group_count,
        "precision_at_5_ppm": i128::from(top5_wins) * i128::from(PROBABILITY_SCALE) / i128::from(top5_rows.max(1)),
        "baseline_precision_at_5_ppm": i128::from(baseline_top5_wins) * i128::from(PROBABILITY_SCALE) / i128::from(top5_rows.max(1)),
        "mean_selected_return_bp": selected_returns / i128::from(selected_return_count.max(1)),
        "baseline_mean_selected_return_bp": baseline_returns / i128::from(selected_return_count.max(1)),
        "multiclass_log_loss_micros": log_loss / row_count,
        "brier_ppm": {
            "down": brier[0] / (row_count * i128::from(PROBABILITY_SCALE)),
            "timeout": brier[1] / (row_count * i128::from(PROBABILITY_SCALE)),
            "up": brier[2] / (row_count * i128::from(PROBABILITY_SCALE)),
        },
        "expected_calibration_error_ppm": {
            "down": ece_numerator[0] / row_count,
            "timeout": ece_numerator[1] / row_count,
            "up": ece_numerator[2] / row_count,
        },
        "up_brier_ppm": brier[2] / (row_count * i128::from(PROBABILITY_SCALE)),
        "up_expected_calibration_error_ppm": ece_numerator[2] / row_count,
    })
}

fn median_timeout_return(groups: &[Vec<NormalizedTrainingRow>]) -> i64 {
    let mut values: Vec<i64> = groups
        .iter()
        .flatten()
        .filter(|row| row.outcome == 1)
        .map(|row| row.outcome_return_bp)
        .collect();
    if values.is_empty() {
        return 0;
    }
    values.sort_unstable();
    let middle = values.len() / 2;
    let median = if values.len() & 1 == 0 {
        rounded_div(i128::from(values[middle - 1] + values[middle]), 2) as i64
    } else {
        values[middle]
    };
    median.clamp(-400, 800)
}

pub fn predict(
    artifact: &IntegerArtifact,
    rows: Vec<PredictionRow>,
) -> Result<Vec<Prediction>, String> {
    validate_artifact(artifact)?;
    let mut predictions: Vec<Prediction> = rows
        .into_iter()
        .map(|row| {
            if row.features.len() != artifact.feature_names.len() {
                return Err(format!("{} has the wrong feature count", row.ticker));
            }
            let normalized = normalize_features(&row.features, &artifact.means, &artifact.scales);
            let probability = probabilities(artifact, &normalized, artifact.temperature_milli);
            Ok(Prediction {
                id: row.id,
                ticker: row.ticker,
                rank: 0,
                probability_down_ppm: probability[0],
                probability_timeout_ppm: probability[1],
                probability_up_ppm: probability[2],
                expected_return_bp: expected_return_bp(probability, artifact.timeout_return_bp),
            })
        })
        .collect::<Result<_, String>>()?;
    let mut order: Vec<usize> = (0..predictions.len()).collect();
    order.sort_by(|left, right| {
        predictions[*right]
            .expected_return_bp
            .cmp(&predictions[*left].expected_return_bp)
            .then_with(|| predictions[*left].ticker.cmp(&predictions[*right].ticker))
    });
    for (rank, index) in order.into_iter().enumerate() {
        predictions[index].rank = rank + 1;
    }
    Ok(predictions)
}

fn validate_artifact(artifact: &IntegerArtifact) -> Result<(), String> {
    let count = artifact.feature_names.len();
    if artifact.schema != ARTIFACT_SCHEMA {
        return Err(format!("unsupported artifact schema: {}", artifact.schema));
    }
    if artifact.feature_scale != FEATURE_SCALE
        || artifact.normalized_scale != NORMALIZED_SCALE
        || artifact.weight_scale != WEIGHT_SCALE
        || artifact.probability_scale != PROBABILITY_SCALE
        || artifact.temperature_scale != TEMPERATURE_SCALE
        || artifact.return_scale != RETURN_SCALE
    {
        return Err("artifact numeric scales do not match this binary".into());
    }
    if artifact.means.len() != count
        || artifact.scales.len() != count
        || artifact.weights.len() != count
        || artifact.weights.iter().any(|row| row.len() != 3)
        || artifact.bias.len() != 3
    {
        return Err("artifact dimensions are invalid".into());
    }
    if artifact.scales.contains(&0) {
        return Err("artifact contains a zero feature scale".into());
    }
    Ok(())
}

fn rounded_div(numerator: i128, denominator: i128) -> i128 {
    debug_assert!(denominator > 0);
    if numerator >= 0 {
        (numerator + denominator / 2) / denominator
    } else {
        -((-numerator + denominator / 2) / denominator)
    }
}

fn integer_sqrt(value: u128) -> u128 {
    if value < 2 {
        return value;
    }
    let mut left = 1_u128;
    let mut right = value.min(1_u128 << 64);
    while left <= right {
        let middle = left + (right - left) / 2;
        if middle <= value / middle {
            left = middle + 1;
        } else {
            right = middle - 1;
        }
    }
    right
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integer_softmax_sums_exactly() {
        let artifact = IntegerArtifact {
            schema: ARTIFACT_SCHEMA.into(),
            feature_names: vec!["x".into()],
            feature_scale: FEATURE_SCALE,
            normalized_scale: NORMALIZED_SCALE,
            weight_scale: WEIGHT_SCALE,
            probability_scale: PROBABILITY_SCALE,
            temperature_scale: TEMPERATURE_SCALE,
            return_scale: RETURN_SCALE,
            means: vec![0],
            scales: vec![1],
            weights: vec![vec![-WEIGHT_SCALE, 0, WEIGHT_SCALE]],
            bias: vec![0, 0, 0],
            temperature_milli: TEMPERATURE_SCALE,
            timeout_return_bp: 0,
        };
        let result = probabilities(&artifact, &[NORMALIZED_SCALE], TEMPERATURE_SCALE);
        assert_eq!(result.iter().sum::<i64>(), PROBABILITY_SCALE);
        assert!(result[2] > result[1]);
        assert!(result[1] > result[0]);
    }

    #[test]
    fn training_and_prediction_are_replayable() {
        let groups: Vec<Vec<TrainingRow>> = (0..8)
            .map(|group| {
                (0..4)
                    .map(|candidate| TrainingRow {
                        ticker: format!("T{candidate}"),
                        features: vec![candidate * FEATURE_SCALE + group % 2],
                        outcome: [0, 1, 2, 2][candidate as usize],
                        outcome_return_bp: [-400, 50, 800, 900][candidate as usize],
                        baseline_score_milli: (4 - candidate) * FEATURE_SCALE,
                    })
                    .collect()
            })
            .collect();
        let first = train(vec!["x".into()], groups.clone(), 80).unwrap();
        let second = train(vec!["x".into()], groups, 80).unwrap();
        assert_eq!(first, second);
        assert_eq!(first["artifact"]["schema"], ARTIFACT_SCHEMA);
        assert_eq!(first["artifact"]["feature_scale"], FEATURE_SCALE);
        let artifact: IntegerArtifact = serde_json::from_value(first["artifact"].clone()).unwrap();
        assert!(artifact.weights.iter().flatten().any(|weight| *weight != 0));
        let predictions = predict(
            &artifact,
            vec![
                PredictionRow {
                    id: "low".into(),
                    ticker: "LOW".into(),
                    features: vec![0],
                },
                PredictionRow {
                    id: "high".into(),
                    ticker: "HIGH".into(),
                    features: vec![3 * FEATURE_SCALE],
                },
            ],
        )
        .unwrap();
        assert!(predictions[1].probability_up_ppm > predictions[0].probability_up_ppm);
    }

    #[test]
    fn training_stops_and_restores_the_best_validation_checkpoint() {
        let groups: Vec<Vec<TrainingRow>> = (0..12)
            .map(|group| {
                let outcomes = if group < 10 { [0, 1, 2] } else { [2, 1, 0] };
                (-1_i64..=1)
                    .enumerate()
                    .map(|(candidate, feature)| TrainingRow {
                        ticker: format!("T{candidate}"),
                        features: vec![feature * FEATURE_SCALE],
                        outcome: outcomes[candidate],
                        outcome_return_bp: [-400, 0, 800][candidate],
                        baseline_score_milli: (3 - candidate as i64) * FEATURE_SCALE,
                    })
                    .collect()
            })
            .collect();

        let result = train(vec!["x".into()], groups, 500).unwrap();
        let control = &result["metrics"]["training_control"];
        let trained_epochs = control["trained_epochs"].as_u64().unwrap();
        let best_epoch = control["best_epoch"].as_u64().unwrap();

        assert_eq!(control["stopped_early"], true);
        assert!(best_epoch < trained_epochs);
        assert!(trained_epochs < 500);
    }

    #[test]
    fn negative_log_is_monotonic() {
        assert!(negative_log_micros(100_000) > negative_log_micros(500_000));
        assert!(negative_log_micros(500_000) > negative_log_micros(900_000));
    }
}

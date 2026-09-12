# Shared market screens

Stocks, Memecoins and Sports use one template, one stylesheet and one interaction script. The market changes the content. List, Detail and Map keep the same layout.

List shows each ticker's name, price or score, and movement or game state. Search covers the saved list. Selecting a row opens Detail. Detail shows one price chart or matchup, followed by the Call action. Map shows saved relationships around each ticker; selecting a ticker opens Detail.

Lists and live games refresh every minute. Refresh keeps search text and keyboard focus. Calls use a confirmation step. The server selects public display fields before rendering the screen or returning a quote or chart.

Source receipts, provider names, model details, logs, credit counters and collection diagnostics belong in internal tools. Keep these fields outside the shared screen contract.

These screenshots were captured from local previews with sample data on September 12, 2026. Desktop previews use a 1280 × 720 viewport; phone previews use 390 × 844.

| Market | List | Detail | Map |
| --- | --- | --- | --- |
| Stocks | [List](stocks-list.png) | [Detail](stocks-detail.png) | [Map](stocks-map.png) |
| Memecoins | [List](memecoins-list.png) | [Detail](memecoins-detail.png) | [Map](memecoins-map.png) |
| Sports | [List](sports-list.png) | [Detail](sports-detail.png) | [Map](sports-map.png) |
| Phone | [List](phone-list.png) | [Detail](phone-detail.png) | [Map](phone-map.png) |

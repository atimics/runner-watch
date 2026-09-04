from __future__ import annotations

import secrets
import unicodedata
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

ADJECTIVES = tuple(
    """
    agile alert amber ancient anxious arcane arctic atomic awake bashful blazing
    blissful bold bouncy brave breezy bright brisk bronze bubbly bullish calm canny
    careful chaotic cheeky chill clever cloudy cosmic crafty cranky crimson crispy
    curious daring dashing dazed degen dizzy dreamy eager electric emerald fancy
    fearless feisty feral fiery flashy fluffy focused foggy frisky frosty funky fuzzy
    gentle giant giddy glassy golden goofy graceful gritty grumpy happy hazy hidden
    horny humble hungry hyper icy itchy jade jazzy jittery jolly juicy jumpy keen lazy
    lilac lively loopy lucky lunar mellow mighty minty moody mossy nimble noisy odd
    onyx peachy perky plucky proud punchy purple quick quiet radiant rapid rascal red
    restless risky rowdy royal salty savage scarlet scrappy sharp shiny sleepy slick
    sly smoky sneaky spicy springy steady stormy sunny swift teal thirsty tiny toasted
    tricky tropical turbo twitchy velvet vivid warm wary wild witty wobbly zany zesty
    """.split()
)

ANIMALS = tuple(
    """
    alpaca anteater antelope armadillo axolotl baboon badger barracuda bat beaver bee
    beetle bison bobcat buffalo butterfly camel capybara caribou cassowary cat caterpillar
    chameleon cheetah chipmunk cobra condor cougar coyote crab crane cricket crocodile
    crow deer dingo dolphin donkey dove dragonfly duck eagle echidna eel elephant elk
    falcon ferret finch firefly flamingo fox frog gecko gerbil giraffe goat goose gorilla
    grasshopper grouse guppy hamster hare hawk hedgehog heron hippo hornet horse hummingbird
    hyena ibis iguana impala jackal jaguar jellyfish kangaroo kingfisher kiwi koala lemur
    leopard lion lizard llama lobster lynx macaque magpie manatee mantis marmot meerkat mole
    mongoose monkey moose moth mouse narwhal newt octopus opossum orangutan orca ostrich
    otter owl ox panda panther parrot peacock pelican penguin pheasant pig pigeon platypus
    porcupine possum puffin puma quail rabbit raccoon ram raven reindeer rhino salamander
    salmon scorpion seal shark sheep shrimp skunk sloth snail sparrow squid squirrel stork
    swan tapir tiger toad toucan turtle walrus wasp weasel whale wolf wombat woodpecker yak
    zebra
    """.split()
)

EMOJIS = tuple(
    "🐺 🦊 🦝 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🦄 🐲 🐙 🦑 🦀 🐡 "
    "🐬 🦈 🐊 🐢 🦎 🐍 🐝 🪲 🦋 🐌 🐞 🐧 🦅 🦉 🦆 🦢 🦜 🦚 🐿️ "
    "🦔 🦦 🦥 🦘 🦬 🦙 🐐 🦌 🐕 🐈 🌵 🍄 🌙 ⭐ ⚡ 🔥 🌊 🍀 🌈 "
    "💎 🧭 🎲 🎯 🛸 🚀 🛰️ 🗿 🎈 🪁 🧩 🎨 🎸 🥁 🛹 🚲 ⛵ 🏔️".split()
)

_COMMENT_GLYPH_RANGES = (
    (0x2200, 0x23FF),
    (0x2500, 0x25FF),
    (0x27C0, 0x27EF),
    (0x2801, 0x28FF),
    (0x2980, 0x2AFF),
)
_MISLEADING_GLYPH_TERMS = (
    "ARROW",
    "PLUS",
    "MINUS",
    "UPWARDS",
    "DOWNWARDS",
    "LEFTWARDS",
    "RIGHTWARDS",
)
COMMENT_GLYPHS = tuple(
    chr(codepoint)
    for start, end in _COMMENT_GLYPH_RANGES
    for codepoint in range(start, end + 1)
    if unicodedata.category(chr(codepoint)) in {"Sm", "So"}
    and not any(term in unicodedata.name(chr(codepoint), "") for term in _MISLEADING_GLYPH_TERMS)
)
_COMMENT_GLYPH_SET = frozenset(COMMENT_GLYPHS)
_EMOJI_ALIASES = tuple(first + second for first in EMOJIS for second in EMOJIS)

AVATAR_TEMPERAMENTS = tuple(
    "agile alert brave bright brisk calm canny careful clever cosmic curious daring earnest "
    "electric exact fearless focused gentle keen lucid nimble patient quiet radiant sharp steady "
    "swift tranquil vivid warm wary witty".split()
)
AVATAR_MATERIALS = tuple(
    "amber azure bronze carbon ceramic chrome cobalt copper coral crystal diamond ember glass "
    "granite ivory jade lunar mint moss neon obsidian pearl quartz ruby sapphire silver solar "
    "steel teal velvet".split()
)
AVATAR_FORMS = tuple(
    "archive beacon cipher circuit comet compass core drone echo engine glimmer guardian lens "
    "mapper mote node oracle orbit prism pulse relay scout sentinel shard signal spark specter "
    "vector warden".split()
)

COMMENT_AVATAR_ABILITIES = (
    {
        "id": "catalyst_scout",
        "label": "Catalyst Scout",
        "description": "Looks first for dated events and confirmed catalysts.",
        "prompt": "Prioritize the strongest dated event or confirmed catalyst in the evidence.",
    },
    {
        "id": "risk_sentinel",
        "label": "Risk Sentinel",
        "description": "Keeps the downside and invalidation in view.",
        "prompt": "Prioritize the clearest downside, blocker, or invalidation in the evidence.",
    },
    {
        "id": "filing_sleuth",
        "label": "Filing Sleuth",
        "description": "Pulls the useful signal from primary filings.",
        "prompt": "Prioritize useful facts from the supplied primary filing evidence.",
    },
    {
        "id": "pattern_mapper",
        "label": "Pattern Mapper",
        "description": "Reads price structure and momentum together.",
        "prompt": "Prioritize the supplied price structure, momentum, and signal evidence.",
    },
    {
        "id": "liquidity_reader",
        "label": "Liquidity Reader",
        "description": "Watches volume, tradability, and crowded moves.",
        "prompt": "Prioritize the supplied volume, liquidity, and trade-state evidence.",
    },
    {
        "id": "countervoice",
        "label": "Countervoice",
        "description": "Tests the obvious story against its strongest counter-case.",
        "prompt": "Prioritize the strongest evidence-backed counter-case to the obvious view.",
    },
)
_COMMENT_AVATAR_ABILITY_BY_ID = {
    str(ability["id"]): ability for ability in COMMENT_AVATAR_ABILITIES
}


def _comment_avatar_name() -> str:
    return " ".join(
        (
            secrets.choice(AVATAR_TEMPERAMENTS).title(),
            secrets.choice(AVATAR_MATERIALS).title(),
            secrets.choice(AVATAR_FORMS).title(),
        )
    )


def comment_avatar_ability(ability_id: str) -> dict[str, str]:
    ability = _COMMENT_AVATAR_ABILITY_BY_ID.get(ability_id)
    if ability is None:
        ability = COMMENT_AVATAR_ABILITIES[0]
    return {key: str(value) for key, value in ability.items()}


def comment_avatar_profile(
    name: str,
    seed: str,
    ability_id: str,
    level: int = 1,
) -> dict[str, Any]:

    digest = sha256(seed.encode()).digest()
    ability = comment_avatar_ability(ability_id)
    return {
        "name": name,
        "ability_id": ability["id"],
        "ability": ability["label"],
        "ability_description": ability["description"],
        "level": max(1, int(level)),
        "tone": digest[0] % 12,
        "frame": digest[1] % 6,
        "eyes": digest[2] % 6,
        "signal": digest[3] % 6,
    }


def ensure_comment_avatar(database: Any, user_id: str) -> dict[str, Any]:

    existing = database.execute(
        "SELECT name,seed,ability_id,level FROM comment_avatars WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if existing:
        return comment_avatar_profile(
            str(existing["name"]),
            str(existing["seed"]),
            str(existing["ability_id"]),
            int(existing["level"]),
        )

    timestamp = datetime.now(UTC).isoformat()
    for _attempt in range(200):
        name = _comment_avatar_name()
        seed = secrets.token_hex(16)
        ability = secrets.choice(COMMENT_AVATAR_ABILITIES)
        database.execute(
            """
            INSERT INTO comment_avatars(user_id,name,seed,ability_id,level,created_at)
            VALUES(?,?,?,?,1,?) ON CONFLICT DO NOTHING
            """,
            (user_id, name, seed, ability["id"], timestamp),
        )
        assigned = database.execute(
            "SELECT name,seed,ability_id,level FROM comment_avatars WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if assigned:
            return comment_avatar_profile(
                str(assigned["name"]),
                str(assigned["seed"]),
                str(assigned["ability_id"]),
                int(assigned["level"]),
            )
    raise RuntimeError("The comment avatar name space is full")


def migrate_comment_aliases_to_glyphs(database: Any) -> int:

    rows = database.execute(
        "SELECT scope,user_id,alias FROM public_aliases "
        "WHERE scope LIKE ? ORDER BY scope,created_at,user_id",
        ("comment:%",),
    ).fetchall()
    scopes: dict[str, list[Any]] = {}
    for row in rows:
        scopes.setdefault(str(row["scope"]), []).append(row)

    changed = 0
    for scope, scoped_rows in scopes.items():
        used = {str(row["alias"]) for row in scoped_rows if str(row["alias"]) in _COMMENT_GLYPH_SET}
        available = [glyph for glyph in COMMENT_GLYPHS if glyph not in used]
        for row in scoped_rows:
            if str(row["alias"]) in _COMMENT_GLYPH_SET:
                continue
            if not available:
                raise RuntimeError(f"The comment glyph space is full for {scope}")
            glyph = available.pop(secrets.randbelow(len(available)))
            database.execute(
                "UPDATE public_aliases SET alias=? WHERE scope=? AND user_id=?",
                (glyph, scope, row["user_id"]),
            )
            changed += 1
    return changed


def ensure_scoped_alias(database: Any, user_id: str, scope: str) -> str:

    if not scope.startswith(("comment:", "call:")):
        raise ValueError("Public alias scope must be a comment or Call thread")
    existing = database.execute(
        "SELECT alias FROM public_aliases WHERE scope=? AND user_id=?",
        (scope, user_id),
    ).fetchone()
    if existing:
        return str(existing["alias"])

    pool = COMMENT_GLYPHS if scope.startswith("comment:") else _EMOJI_ALIASES
    used = {
        str(row["alias"])
        for row in database.execute(
            "SELECT alias FROM public_aliases WHERE scope=?",
            (scope,),
        ).fetchall()
    }
    available = [alias for alias in pool if alias not in used]
    timestamp = datetime.now(UTC).isoformat()
    while available:
        alias = available.pop(secrets.randbelow(len(available)))
        database.execute(
            """
            INSERT INTO public_aliases(scope,user_id,alias,created_at)
            VALUES(?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (scope, user_id, alias, timestamp),
        )
        assigned = database.execute(
            "SELECT alias FROM public_aliases WHERE scope=? AND user_id=?",
            (scope, user_id),
        ).fetchone()
        if assigned:
            return str(assigned["alias"])
    raise RuntimeError(f"The public alias space is full for {scope}")

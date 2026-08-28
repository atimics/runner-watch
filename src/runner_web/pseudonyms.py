from __future__ import annotations

import secrets
import unicodedata
from datetime import UTC, datetime
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
    (0x2200, 0x23FF),  # mathematical and technical symbols
    (0x2500, 0x25FF),  # box, block, and geometric symbols
    (0x27C0, 0x27EF),  # miscellaneous mathematical symbols
    (0x2801, 0x28FF),  # non-empty braille patterns
    (0x2980, 0x2AFF),  # supplemental mathematical symbols
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


def migrate_comment_aliases_to_glyphs(database: Any) -> int:
    """Replace old comment emoji pairs with unique one-glyph thread aliases."""

    rows = database.execute(
        "SELECT scope,user_id,alias FROM public_aliases "
        "WHERE scope LIKE 'comment:%' ORDER BY scope,created_at,user_id"
    ).fetchall()
    scopes: dict[str, list[Any]] = {}
    for row in rows:
        scopes.setdefault(str(row["scope"]), []).append(row)

    changed = 0
    for scope, scoped_rows in scopes.items():
        used = {
            str(row["alias"])
            for row in scoped_rows
            if str(row["alias"]) in _COMMENT_GLYPH_SET
        }
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
    """Assign a random public pseudonym inside one discussion or Call thread."""

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

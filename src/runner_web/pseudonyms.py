from __future__ import annotations

import hashlib
import os

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

PSEUDONYM_SALT = os.getenv(
    "COMMENT_PSEUDONYM_SALT",
    "runner-watch-comment-pseudonym-v1",
)


def pseudonym_candidate(identity: str, attempt: int = 0) -> str:
    """Choose a stable adjective-animal name without exposing the identity."""

    digest = hashlib.sha256(f"{PSEUDONYM_SALT}:{identity}:{attempt}".encode()).digest()
    adjective_index = int.from_bytes(digest[:8], "big") % len(ADJECTIVES)
    animal_index = int.from_bytes(digest[8:16], "big") % len(ANIMALS)
    return f"{ADJECTIVES[adjective_index]}-{ANIMALS[animal_index]}"

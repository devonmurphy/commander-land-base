#!/usr/bin/env python3
"""
Commander Land Base Generator
------------------------------
Give it a commander name, and it builds a full land base, never exceeding
your requested total land count:

  1. Gathers candidate nonbasic lands from Scryfall (original duals, shocks,
     fetches, Battle Bond lands, filter lands that fit the commander's color
     identity) and from EDHREC's "Lands" and "Utility Lands" categories.
  2. Ranks the whole combined pool by inclusion rate (highest first).
  3. Auto-includes Command Beacon and Ancient Tomb always, plus Command
     Tower if the commander's color identity is 2+ colors.
  4. Reserves room for a minimum number of each basic land type, then fills
     the rest of the deck with the highest-ranked nonbasics that fit --
     anything that doesn't fit gets cut, favoring popularity over quantity.
  5. Fills any leftover slots with basics.

  --lands is a hard cap: the final list will never exceed it.

Usage:
    python commander_lands.py "Korvold, Fae-Cursed King"
    python commander_lands.py "Korvold, Fae-Cursed King" --lands 38
    python commander_lands.py "Korvold, Fae-Cursed King" -m 2
    python commander_lands.py                              (prompts for everything)

Both Scryfall (https://scryfall.com) and EDHREC (https://edhrec.com) are
free and don't require an API key. EDHREC doesn't publish an official
API, so this uses the same JSON endpoint their own website's frontend
calls -- it's stable and widely used by community tools, but could
change without notice since it's unofficial.
"""

import argparse
import re
import requests
import subprocess
import sys
import time

SCRYFALL_ROOT = "https://api.scryfall.com"
EDHREC_ROOT = "https://json.edhrec.com/pages/commanders"

HEADERS = {
    # Scryfall asks that you set a descriptive User-Agent
    "User-Agent": "CommanderLandBuilder/1.0 (personal use script)",
    "Accept": "application/json",
}

DEFAULT_TOTAL_LANDS = 37
DEFAULT_EDHREC_POOL = 10     # how many top EDHREC lands to pull in as ranking candidates
DEFAULT_MIN_BASICS = 1       # minimum copies of each basic land type, if it fits

# Utility land categories to search for on Scryfall. Each is a Scryfall
# `is:` search tag -- see https://scryfall.com/docs/syntax
LAND_CATEGORIES = {
    "Dual Lands (original)": "is:dual",
    "Shock Lands": "is:shockland",
    "Fetch Lands": "is:fetchland",
    "Battle Bond Lands": "is:bondland",
}

BASIC_LAND_BY_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}
SNOW_BASIC_LAND_BY_COLOR = {
    "W": "Snow-Covered Plains",
    "U": "Snow-Covered Island",
    "B": "Snow-Covered Swamp",
    "R": "Snow-Covered Mountain",
    "G": "Snow-Covered Forest",
}
BASIC_LAND_NAMES = (
    set(BASIC_LAND_BY_COLOR.values())
    | set(SNOW_BASIC_LAND_BY_COLOR.values())
    | {"Wastes", "Snow-Covered Wastes"}
)

# Lands that always get an auto-included slot (ahead of the ranked EDHREC/
# Scryfall pool), regardless of inclusion rate.
ALWAYS_INCLUDE_LANDS = ["Command Beacon", "Ancient Tomb"]
# Additional auto-include for commanders with 2+ colors in their identity.
MULTICOLOR_INCLUDE_LAND = "Command Tower"


def get_forced_lands(colors):
    """Lands that are always auto-included, ahead of the ranked pool."""
    forced = list(ALWAYS_INCLUDE_LANDS)
    if len(colors) >= 2:
        forced.insert(0, MULTICOLOR_INCLUDE_LAND)
    return forced


# ---------------------------------------------------------------------------
# Scryfall
# ---------------------------------------------------------------------------

class CommanderNotFoundError(Exception):
    """Raised when Scryfall has no card matching the given name."""


class NotACommanderError(Exception):
    """Raised when the resolved card can't legally lead a Commander deck."""


def get_commander(name):
    """Look up a commander by (fuzzy) name and return its Scryfall card object."""
    resp = requests.get(
        f"{SCRYFALL_ROOT}/cards/named",
        params={"fuzzy": name},
        headers=HEADERS,
        timeout=10,
    )
    if resp.status_code == 404:
        raise CommanderNotFoundError(
            f"Couldn't find a card named '{name}'. Check the spelling?"
        )
    resp.raise_for_status()
    return resp.json()


def is_valid_commander(real_name):
    """True if Scryfall's own `is:commander` filter accepts this exact card
    -- i.e. it's a legendary creature (or has an explicit "can be your
    commander" ability), not just any card. Delegates to Scryfall instead
    of reimplementing that legality logic ourselves."""
    resp = requests.get(
        f"{SCRYFALL_ROOT}/cards/search",
        params={"q": f'is:commander !"{real_name}"'},
        headers=HEADERS,
        timeout=10,
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return bool(resp.json().get("data"))


def get_card_image(card):
    """Best available card image URL, handling double-faced cards (whose
    images live under card_faces instead of top-level image_uris)."""
    image_uris = card.get("image_uris")
    if image_uris:
        return image_uris.get("normal") or image_uris.get("large") or image_uris.get("small")
    for face in card.get("card_faces") or []:
        face_images = face.get("image_uris")
        if face_images:
            return face_images.get("normal") or face_images.get("large") or face_images.get("small")
    return None


def get_card_face_images(card):
    """Image URL per face, in order, for cards with separate face art
    (transform/modal double-faced cards) -- otherwise a single-item list
    with the card's one image (or an empty list if no art is available).
    Used for the commander portrait so the UI can offer a flip button."""
    faces = card.get("card_faces") or []
    face_urls = []
    for face in faces:
        image_uris = face.get("image_uris")
        if image_uris:
            face_urls.append(
                image_uris.get("normal") or image_uris.get("large") or image_uris.get("small")
            )
    face_urls = [url for url in face_urls if url]
    if face_urls:
        return face_urls
    single = get_card_image(card)
    return [single] if single else []


def get_card_images(names):
    """Batch-fetch image URLs for a list of card names via Scryfall's
    /cards/collection endpoint (up to 75 identifiers per request). Returns
    {name: image_url}, silently omitting any name Scryfall couldn't find.

    Double-faced cards come back with a combined "Front // Back" name even
    though callers ask (and look up) by a single face's name -- match the
    returned card back to whichever requested name it actually satisfies
    (the combined name or either face) instead of always keying by
    card["name"], or DFC lands would never resolve to an image."""
    images = {}
    unique_names = list(dict.fromkeys(names))  # de-dup, preserve order
    name_set = set(unique_names)
    for i in range(0, len(unique_names), 75):
        batch = unique_names[i:i + 75]
        resp = requests.post(
            f"{SCRYFALL_ROOT}/cards/collection",
            headers=HEADERS,
            json={"identifiers": [{"name": n} for n in batch]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        for card in data.get("data", []):
            url = get_card_image(card)
            if not url:
                continue
            possible_keys = [card["name"]] + [
                face.get("name") for face in card.get("card_faces") or []
            ]
            matched_key = next((k for k in possible_keys if k in name_set), card["name"])
            images[matched_key] = url
    return images


def search_all(query):
    """Run a Scryfall search query and return every card across all pages."""
    cards = []
    url = f"{SCRYFALL_ROOT}/cards/search"
    params = {"q": query, "order": "name", "unique": "cards"}

    while url:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if resp.status_code == 404:
            break  # no matches -- not an error, just empty
        resp.raise_for_status()
        data = resp.json()
        cards.extend(data.get("data", []))

        if data.get("has_more"):
            url = data["next_page"]
            params = None  # next_page URL already has query params baked in
            time.sleep(0.1)  # be polite to the free API
        else:
            url = None

    return cards


def color_identity_query(colors):
    if not colors:
        return "id=c"
    return f"id<={''.join(colors)}"


def build_land_query(colors, category_query):
    id_clause = color_identity_query(colors)
    return f"type:land {id_clause} ({category_query})"


def build_fetch_land_query(colors):
    """Fetch lands (Windswept Heath, etc.) have an EMPTY color_identity on
    Scryfall -- they name basic land types in plain oracle text rather than
    with colored mana symbols, so `id<=colors` matches every fetch land for
    every commander regardless of colors. Filter on which basics they can
    actually find instead. Returns None for colorless commanders (no basics
    to fetch)."""
    if not colors:
        return None
    basic_clause = " or ".join(f'o:"{BASIC_LAND_BY_COLOR[c]}"' for c in colors)
    return f"type:land is:fetchland ({basic_clause})"


def get_utility_lands(colors):
    """Return {category_name: [card names]} for every LAND_CATEGORIES entry."""
    results = {}
    for category_name, category_query in LAND_CATEGORIES.items():
        if category_name == "Fetch Lands":
            query = build_fetch_land_query(colors)
        else:
            query = build_land_query(colors, category_query)

        cards = search_all(query) if query else []
        seen = []
        for card in cards:
            if card["name"] not in seen:
                seen.append(card["name"])
        results[category_name] = seen
    return results


# ---------------------------------------------------------------------------
# EDHREC
# ---------------------------------------------------------------------------

def edhrec_slug(name):
    """Turn a card name into EDHREC's URL slug format.
    e.g. "Korvold, Fae-Cursed King" -> "korvold-fae-cursed-king" """
    slug = name.lower()
    slug = slug.replace("'", "").replace(",", "")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def fetch_edhrec_cardlists(commander_name, warn=print):
    """Fetch a commander's EDHREC page once and return its raw `cardlists`
    array (list of {header, tag, cardviews}), or None if unavailable.
    `warn` is called with any diagnostic message instead of printing
    directly, so callers (CLI, web UI) can route it wherever they like."""
    slug = edhrec_slug(commander_name)
    url = f"{EDHREC_ROOT}/{slug}.json"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 404:
            warn(f"  (EDHREC has no page for '{commander_name}' -- skipping)")
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        warn(f"  (Couldn't reach EDHREC: {e} -- skipping)")
        return None

    try:
        return data["container"]["json_dict"]["cardlists"]
    except (KeyError, TypeError):
        warn("  (Unexpected EDHREC response format -- skipping)")
        return None


def extract_top_lands(cardlists, tag, pool_size=DEFAULT_EDHREC_POOL, warn=print):
    """Pull the top `pool_size` nonbasic entries for a given cardlist `tag`
    (e.g. "lands" for the general Lands category, "utilitylands" for the
    Utility Lands category) out of an already-fetched `cardlists` array.
    Returns a list of (name, num_decks, potential_decks), highest-inclusion
    first. This is a *pool* to pick new lands from -- the caller decides how
    many to actually keep after deduping against lands already in the list."""
    if not cardlists:
        return []

    target_list = next((cl for cl in cardlists if cl.get("tag") == tag), None)
    if not target_list:
        warn(f"  (No '{tag}' category found on EDHREC for this commander)")
        return []

    pool = []
    for cardview in target_list.get("cardviews", []):
        name = cardview.get("name")
        if not name or name in BASIC_LAND_NAMES:
            continue  # basics are handled separately
        pool.append((name, cardview.get("num_decks", 0), cardview.get("potential_decks", 0)))
        if len(pool) >= pool_size:
            break

    return pool


# ---------------------------------------------------------------------------
# Clipboard / output
# ---------------------------------------------------------------------------

def copy_to_clipboard(text):
    """Copy text to the system clipboard, trying Windows/Mac/Linux tools in turn."""
    commands = [
        ["clip.exe"],          # Windows / Git Bash / WSL
        ["clip"],              # Windows cmd
        ["pbcopy"],            # macOS
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]
    for cmd in commands:
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def distribute_basics(colors, remaining, min_per_color=DEFAULT_MIN_BASICS, use_snow=False):
    """Split `remaining` land slots across the commander's basic land types.
    This NEVER returns more than `remaining` total lands -- it's a hard cap.
    Every type gets `min_per_color` first if there's room; if there isn't
    enough room to give everyone the minimum, it falls back to a plain even
    split of whatever's available (which may be below the minimum) and
    reports that the minimum wasn't fully honored.
    Colorless commanders get Wastes (or Snow-Covered Wastes). Returns
    (counts_dict, minimum_honored)."""
    remaining = max(remaining, 0)
    land_by_color = SNOW_BASIC_LAND_BY_COLOR if use_snow else BASIC_LAND_BY_COLOR
    wastes = "Snow-Covered Wastes" if use_snow else "Wastes"
    basics = [land_by_color[c] for c in colors] if colors else [wastes]
    n = len(basics)
    reserved = min_per_color * n

    if remaining >= reserved:
        # Give everyone the minimum, then spread whatever's left evenly.
        extra_pool = remaining - reserved
        base, extra = divmod(extra_pool, n)
        counts = {
            land: min_per_color + base + (1 if i < extra else 0)
            for i, land in enumerate(basics)
        }
        return counts, True

    # Not enough room to honor the minimum without busting the cap --
    # just split what's actually available.
    base, extra = divmod(remaining, n)
    counts = {land: base + (1 if i < extra else 0) for i, land in enumerate(basics)}
    return counts, False


# ---------------------------------------------------------------------------
# Core generation (shared by the CLI and the web UI)
# ---------------------------------------------------------------------------

def inclusion_pct(data):
    """data is (num_decks, potential_decks) or None. Returns 0.0-1.0."""
    if not data or not data[1]:
        return 0.0
    return data[0] / data[1]


def generate_land_base(commander_name, total_lands,
                        edhrec_pool=DEFAULT_EDHREC_POOL,
                        utility_pool=DEFAULT_EDHREC_POOL,
                        min_basics=DEFAULT_MIN_BASICS,
                        use_snow_basics=False,
                        log=print):
    """Run the full pipeline (Scryfall + EDHREC gathering, ranking, auto-
    includes, basics) for one commander and return a dict with every piece
    a caller might want to display. `log` is called with progress/diagnostic
    messages as they happen -- pass a no-op or a list.append to capture them
    instead of printing. Raises CommanderNotFoundError if Scryfall doesn't
    recognize the commander name, or NotACommanderError if it resolves to a
    real card that isn't actually legal to lead a Commander deck."""
    log(f"\nLooking up '{commander_name}' on Scryfall...")
    commander = get_commander(commander_name)
    real_name = commander["name"]

    if not is_valid_commander(real_name):
        raise NotACommanderError(
            f"'{real_name}' can't be your commander -- it's not a legendary "
            f"creature (or other card) with a \"can be your commander\" ability."
        )

    colors = commander.get("color_identity", [])
    color_str = "".join(colors) if colors else "Colorless"
    image_urls = get_card_face_images(commander)

    log(f"Found: {real_name}")
    log(f"Color Identity: {color_str}")
    log(f"Target land count: {total_lands}\n")
    log("=" * 50)

    # --- 1. Gather candidate nonbasic lands from Scryfall (categorized,
    #        just for display -- these don't get auto-included yet) ---
    utility_by_category = get_utility_lands(colors)
    candidates = {}  # name -> (num_decks, potential_decks) or None if no EDHREC data

    for category_name, names in utility_by_category.items():
        log(f"\n{category_name} ({len(names)}):")
        if not names:
            log("  (none in this color identity)")
            continue
        for name in names:
            log(f"  - {name}")
            candidates.setdefault(name, None)

    # --- 2. Pull ranking candidates from EDHREC's "Lands" and "Utility
    #        Lands" categories (fetched once, both categories read from it) ---
    log(f"\nFetching EDHREC data for '{real_name}'...")
    cardlists = fetch_edhrec_cardlists(real_name, warn=log)
    edhrec_lands = extract_top_lands(cardlists, "lands", edhrec_pool, warn=log)
    edhrec_utility = extract_top_lands(cardlists, "utilitylands", utility_pool, warn=log)

    for name, num_decks, potential in edhrec_lands + edhrec_utility:
        candidates[name] = (num_decks, potential)  # overwrite None with real data

    log(f"Pulled {len(edhrec_lands)} candidates from EDHREC Lands, "
        f"{len(edhrec_utility)} from EDHREC Utility Lands.")
    log(f"Total unique nonbasic candidates: {len(candidates)}")

    # --- 3. Rank the whole combined pool by inclusion rate, highest first.
    #        Candidates with no EDHREC data (Scryfall-only finds that didn't
    #        crack the top of either EDHREC list) sort after everything with
    #        known data, since we have nothing to rank them by. ---
    ranked = sorted(
        candidates.items(),
        key=lambda kv: (kv[1] is None, -inclusion_pct(kv[1]), kv[0])
    )

    # --- 4. --lands is a hard cap. Reserve room for the basic-land minimum
    #        and the always-include lands first, then fill the rest with the
    #        highest-ranked nonbasics. ---
    forced_lands = get_forced_lands(colors)[:total_lands]  # never bust the cap
    ranked = [(name, data) for name, data in ranked if name not in forced_lands]

    num_basic_types = len(colors) if colors else 1
    reserved_for_basics = min_basics * num_basic_types
    nonbasic_slots = max(total_lands - reserved_for_basics - len(forced_lands), 0)

    kept = ranked[:nonbasic_slots]
    cut = ranked[nonbasic_slots:]
    nonbasic_names = forced_lands + [name for name, _ in kept]

    # --- 5. Fill remaining slots with basics (never exceeds the cap) ---
    remaining_for_basics = total_lands - len(nonbasic_names)
    basics, min_honored = distribute_basics(
        colors, remaining_for_basics, min_basics, use_snow=use_snow_basics
    )
    basics_total = sum(basics.values())

    total_output = len(nonbasic_names) + basics_total
    assert total_output == total_lands, "internal error: land count drifted from the cap"

    # --- Build the Archidekt-style import list ---
    decklist_lines = [f"1x {name}" for name in sorted(nonbasic_names)]
    decklist_lines += [f"{count}x {land}" for land, count in basics.items()]
    decklist_text = "\n".join(decklist_lines)

    # --- Fetch card images for everything we're about to display ---
    log("\nFetching card images...")
    image_names = (
        forced_lands
        + [name for name, _ in kept]
        + [name for name, _ in cut]
        + list(basics.keys())
    )
    land_images = get_card_images(image_names)

    return {
        "commander_name": commander_name,
        "real_name": real_name,
        "colors": colors,
        "color_str": color_str,
        "image_urls": image_urls,
        "land_images": land_images,
        "total_lands": total_lands,
        "min_basics": min_basics,
        "use_snow_basics": use_snow_basics,
        "utility_by_category": utility_by_category,
        "edhrec_lands_count": len(edhrec_lands),
        "edhrec_utility_count": len(edhrec_utility),
        "total_candidates": len(candidates),
        "forced_lands": forced_lands,
        "kept": kept,
        "cut": cut,
        "ranked_remaining_count": len(ranked),
        "nonbasic_names": nonbasic_names,
        "basics": basics,
        "basics_total": basics_total,
        "min_honored": min_honored,
        "total_output": total_output,
        "decklist_text": decklist_text,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a commander land base using Scryfall + EDHREC."
    )
    parser.add_argument(
        "commander", nargs="*", help="Commander name (quotes recommended)"
    )
    parser.add_argument(
        "-l", "--lands", type=int, default=None,
        help=f"Total lands in the deck -- a hard cap, never exceeded "
             f"(default {DEFAULT_TOTAL_LANDS})"
    )
    parser.add_argument(
        "-p", "--edhrec-pool", type=int, default=DEFAULT_EDHREC_POOL,
        help=f"How many of EDHREC's top Lands to pull in as ranking "
             f"candidates (default {DEFAULT_EDHREC_POOL})"
    )
    parser.add_argument(
        "-U", "--utility-pool", type=int, default=DEFAULT_EDHREC_POOL,
        help=f"How many of EDHREC's top Utility Lands to pull in as ranking "
             f"candidates (default {DEFAULT_EDHREC_POOL})"
    )
    parser.add_argument(
        "-m", "--min-basics", type=int, default=DEFAULT_MIN_BASICS,
        help=f"Minimum copies of each basic land type, honored as long as "
             f"it fits within --lands (default {DEFAULT_MIN_BASICS})"
    )
    parser.add_argument(
        "-s", "--snow-basics", action="store_true",
        help="Use Snow-Covered basics instead of regular ones"
    )
    args = parser.parse_args()

    commander_name = " ".join(args.commander).strip()
    if not commander_name:
        commander_name = input("Enter your commander's name: ").strip()
    if not commander_name:
        print("No commander name given.")
        sys.exit(1)

    total_lands = args.lands
    if total_lands is None:
        raw = input(
            f"Total lands in deck [default {DEFAULT_TOTAL_LANDS}]: "
        ).strip()
        total_lands = int(raw) if raw else DEFAULT_TOTAL_LANDS

    try:
        result = generate_land_base(
            commander_name, total_lands,
            edhrec_pool=args.edhrec_pool,
            utility_pool=args.utility_pool,
            min_basics=args.min_basics,
            use_snow_basics=args.snow_basics,
        )
    except (CommanderNotFoundError, NotACommanderError) as e:
        print(str(e))
        sys.exit(1)

    print("\n" + "=" * 50)
    print(f"Final nonbasic lands ({len(result['nonbasic_names'])} total -- "
          f"{len(result['forced_lands'])} auto-included, {len(result['kept'])} of "
          f"{result['ranked_remaining_count']} remaining candidates ranked by "
          f"inclusion rate):")
    for name in result["forced_lands"]:
        print(f"  - {name}  [auto-included]")
    for name, data in result["kept"]:
        pct = f"{inclusion_pct(data) * 100:.0f}%" if data else "no EDHREC data"
        print(f"  - {name}  [{pct}]")

    cut = result["cut"]
    if cut:
        print(f"\nCut to fit your {total_lands}-land cap ({len(cut)} lands):")
        for name, data in cut[:10]:
            pct = f"{inclusion_pct(data) * 100:.0f}%" if data else "no EDHREC data"
            print(f"  - {name}  [{pct}]")
        if len(cut) > 10:
            print(f"  ...and {len(cut) - 10} more")

    print("\n" + "=" * 50)
    print(f"Basic lands: {result['basics_total']}")
    for land, count in result["basics"].items():
        print(f"  - {count}x {land}")
    if not result["min_honored"]:
        print(f"Note: couldn't fully honor the {args.min_basics}-per-color "
              f"minimum within your {total_lands}-land cap -- gave basics as "
              f"even a split as possible instead.")

    print("\n" + "=" * 50)
    print(f"Archidekt import list ({result['total_output']} lands):\n")
    print(result["decklist_text"])

    if copy_to_clipboard(result["decklist_text"]):
        print("\n(Copied to clipboard — paste directly into Archidekt's deck importer)")
    else:
        print("\n(Couldn't find a clipboard tool to copy automatically — "
              "just select and copy the list above manually)")


if __name__ == "__main__":
    main()
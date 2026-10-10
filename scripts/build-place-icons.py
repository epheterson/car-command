#!/usr/bin/env python3
"""Build stack/web/static/icons/places.json, the place icons, from a lucide-static package.

    curl -sSL https://registry.npmjs.org/lucide-static/-/lucide-static-1.52.0.tgz | tar xz
    python3 scripts/build-place-icons.py package

The file maps each icon key to [title, inner SVG markup, search words]. The first keys are
the ones places already store (cc_places.icon): each keeps its name and takes a Lucide
glyph (LEGACY), so no stored place loses its icon. Then a curated subset of Lucide by
group (GROUPS), each under its Lucide name. Lucide is ISC licensed (static/icons/LICENSE).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "stack" / "web" / "static" / "icons" / "places.json"

# Stored key -> (Lucide glyph, title, extra search words). "home" is the one key that
# makes a place home (places.is_home); its glyph is Lucide's house.
LEGACY: dict[str, tuple[str, str, str]] = {
    "home": ("house", "Home", "home house"),
    "work": ("briefcase", "Work, office", "work office job"),
    "family": ("users", "Family", "family parents mom dad"),
    "friend": ("user", "Friend", "friend person"),
    "kids": ("baby", "Daycare, kids", "kids daycare preschool child nursery"),
    "school": ("school", "School", "school elementary"),
    "shop": ("shopping-cart", "Grocery", "grocery supermarket market"),
    "store": ("shopping-bag", "Shopping", "shopping store mall"),
    "food": ("utensils", "Restaurant", "restaurant food eat dinner lunch"),
    "coffee": ("coffee", "Coffee", "coffee cafe"),
    "bar": ("martini", "Bar", "bar pub drinks cocktail"),
    "gym": ("dumbbell", "Gym", "gym fitness workout"),
    "doctor": ("stethoscope", "Doctor, hospital", "doctor clinic medical"),
    "dentist": ("", "Dentist", "dentist dental teeth tooth orthodontist"),
    "pharmacy": ("pill", "Pharmacy", "pharmacy drugstore medicine"),
    "pet": ("paw-print", "Vet, pet", "vet pet veterinary groomer"),
    "service": ("wrench", "Car service", "car service mechanic repair"),
    "gas": ("fuel", "Gas", "gas petrol fuel station"),
    "charge": ("zap", "EV charger", "charger charging ev supercharger electric"),
    "parking": ("square-parking", "Parking", "parking car park lot"),
    "park": ("tree-pine", "Park", "park trees trail"),
    "beach": ("parasol", "Beach", "beach sea shore"),
    "mountain": ("mountain-snow", "Mountain, ski", "mountain ski snow"),
    "venue": ("ticket", "Stadium, venue", "stadium venue arena concert event"),
    "worship": ("church", "Church", "worship church chapel"),
    "library": ("library", "Library", "library books"),
    "hotel": ("bed-double", "Hotel", "hotel motel inn stay sleep"),
    "travel": ("plane", "Airport, travel", "airport travel flight"),
    "transit": ("train-front", "Train, transit", "train transit station metro subway"),
    "star": ("star", "Star", "star favourite favorite"),
    "pin": ("map-pin", "Pin", "pin place location"),
}

# Lucide has no tooth: the dentist keeps its own glyph, drawn in the same 24-unit line style.
CUSTOM = {
    "dentist": '<path d="M7.5 4C5.2 4 3.7 5.9 4 8.4c.3 2.3 1.4 3.6 1.9 5.6.5 2.2.8 6 2.4 6 1.4 0 1.5-4.5 3.7-4.5s2.3 4.5 3.7 4.5c1.6 0 1.9-3.8 2.4-6 .5-2 1.6-3.3 1.9-5.6.3-2.5-1.2-4.4-3.5-4.4-1.8 0-2.6 1-4.5 1S9.3 4 7.5 4z"/>',
}

# A Lucide icon whose name is an old key but whose glyph is not that key's: kept under another name.
RENAMED = {"store": "storefront"}

# The curated subset, by group; the group's name joins each icon's search words.
GROUPS: dict[str, str] = {
    "home": "house-heart house-plus house-plug houses map-pin-house building building-complex castle tent caravan door-open sofa armchair bed bed-single bath lamp key-round fence",
    "work": "briefcase-business factory warehouse forklift laptop monitor computer presentation calendar clipboard hard-hat server printer id-card-lanyard landmark storefront pen-tool container",
    "food": "utensils-crossed chef-hat pizza sandwich salad soup hamburger beef drumstick fish shrimp croissant cake cake-slice cupcake cookie donut ice-cream-cone ice-cream-bowl popsicle candy lollipop dessert egg-fried cup-soda milk beer wine glass-water bottle-wine citrus apple cherry grape banana carrot wheat cooking-pot popcorn concierge-bell hand-platter vegan leafy-green",
    "shop": "shopping-basket handbag shirt gift gem glasses flower flower-2 book scissors receipt tag package box hammer drill paint-roller paintbrush palette smartphone headphones watch sport-shoe toolbox tool-case wallet credit-card banknote piggy-bank coins",
    "health": "hospital pill-bottle syringe heart-pulse activity ambulance cross bandage thermometer brain eye ear bone microscope test-tube tablets accessibility hand-heart toothbrush briefcase-medical scan-heart",
    "family kids": "users-round user-round blocks toy-brick puzzle balloon party-popper rocking-chair person-standing heart heart-handshake contact face-slightly-smiling",
    "school": "graduation-cap book-open backpack pencil notebook library-big calculator music piano guitar drum university lectern",
    "sports": "bike volleyball rugby-ball trophy medal goal biceps-flexed waves-ladder sailboat kayak fishing-rod footprints flag target dices chess-knight gamepad-2 joystick",
    "outdoors": "trees tree-deciduous tree-palm tent-tree flame-kindling waves-horizontal sun sunrise sunset umbrella leaf sprout shrub snowflake compass map binoculars telescope tractor lighthouse anchor ship-wheel bridge dam milestone signpost earth globe",
    "travel": "plane-takeoff plane-landing luggage baggage-claim tickets-plane map-pinned ship camera",
    "transport": "car car-front car-taxi-front bus bus-front train-front-tunnel tram-front train-track cable-car scooter motorbike truck van helicopter rocket traffic-cone road route circle-parking parking-meter ev-charger plug-zap battery-charging car-battery gauge tower-control construction",
    "services auto": "engine cog droplets spray-can washing-machine mail mailbox scale gavel shield siren flame fire-extinguisher recycle trash phone wifi key lock vault stamp newspaper radio-tower satellite-dish shield-check",
    "worship": "mosque moon-star hand-helping",
    "entertainment": "theater drama clapperboard film music-2 mic-vocal ferris-wheel roller-coaster spade club sparkles tv speaker disc-3 podium tickets",
    "animals pets": "dog cat bird rabbit rat squirrel turtle snail panda bug worm egg birdhouse",
    "other": "crown award ribbon bookmark bell clock hourglass lightbulb moon cloud droplet circle",
}


def markup(nodes: list) -> str:
    return "".join("<" + tag + "".join(f' {k}="{v}"' for k, v in attrs.items() if k != "key") + "/>" for tag, attrs in nodes)


def words(*parts: str) -> str:
    seen: list[str] = []
    for part in parts:
        for w in part.lower().replace("-", " ").split():
            if w not in seen:
                seen.append(w)
    return " ".join(seen)


def build(pkg: Path) -> dict[str, list[str]]:
    nodes = json.loads((pkg / "icon-nodes.json").read_text())
    tags = json.loads((pkg / "tags.json").read_text())
    out: dict[str, list[str]] = {}
    for key, (glyph, title, extra) in LEGACY.items():
        inner = CUSTOM.get(key) or markup(nodes[glyph])
        out[key] = [title, inner, words(extra, glyph, " ".join(tags.get(glyph, [])))]
    taken = {g for g, _, _ in LEGACY.values()} | set(LEGACY)
    for group, names in GROUPS.items():
        for name in names.split():
            source = next((k for k, v in RENAMED.items() if v == name), name)
            if name in out or (name in taken and source == name):
                continue
            if source not in nodes:
                raise SystemExit(f"no Lucide icon {source!r}")
            title = name.replace("-", " ").capitalize()
            out[name] = [title, markup(nodes[source]), words(name, source, group, " ".join(tags.get(source, [])))]
    return out


if __name__ == "__main__":
    icons = build(Path(sys.argv[1]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(icons, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"{len(icons)} icons, {OUT.stat().st_size} bytes -> {OUT}")

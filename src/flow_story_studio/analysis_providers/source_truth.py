"""Deterministic screenplay source-truth compiler.

This module converts screenplay evidence into typed physical state. AI prose may enrich
cinematography, but it cannot redefine temporal, perceptual or prop lifecycle facts.
"""

from __future__ import annotations

import re
import unicodedata

from ..models import (
    Character,
    Location,
    Prop,
    PropEvent,
    PropPhysicalState,
    Scene,
    SceneSemanticTruth,
    SceneTemporalState,
)

_CONTEXT_RE = re.compile(r"\[SCENE CONTEXT\](.*?)\[END CONTEXT\]", re.DOTALL)
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")

_PERCEPTUAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("trong doan video cu", "cctv"),
    ("trong đoạn video cũ", "cctv"),
    ("trong footage cu", "cctv"),
    ("trong footage cũ", "cctv"),
    ("in the security footage", "cctv"),
    ("in the footage", "cctv"),
    ("security footage", "cctv"),
    ("trong video", "screen"),
    ("video cu", "screen"),
    ("video cũ", "screen"),
    ("tren man hinh", "screen"),
    ("trên màn hình", "screen"),
    ("on the screen", "screen"),
    ("in the photo", "photo"),
    ("trong guong", "mirror"),
    ("trong gương", "mirror"),
    ("in the mirror", "mirror"),
    ("qua video call", "phone_video"),
    ("phone video", "phone_video"),
)

_DAYPARTS = (
    ("binh minh", "dawn"),
    ("bình minh", "dawn"),
    ("sang", "morning"),
    ("sáng", "morning"),
    ("trua", "day"),
    ("trưa", "day"),
    ("chieu", "afternoon"),
    ("chiều", "afternoon"),
    ("hoang hon", "dusk"),
    ("hoàng hôn", "dusk"),
    ("toi", "evening"),
    ("tối", "evening"),
    ("dem", "night"),
    ("đêm", "night"),
    ("morning", "morning"),
    ("afternoon", "afternoon"),
    ("evening", "evening"),
    ("night", "night"),
    ("dawn", "dawn"),
    ("dusk", "dusk"),
    ("day", "day"),
)


def key(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").casefold().replace("đ", "d"))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", raw))


def scene_context(scene: Scene) -> str:
    match = _CONTEXT_RE.search(scene.source_text)
    return " ".join((match.group(1) if match else "").split())


def scene_body(scene: Scene) -> str:
    return _CONTEXT_RE.sub(" ", scene.source_text).strip()


def _dialogue_stripped_body(scene: Scene) -> str:
    """Remove immutable spoken payload before inferring physical-world prop facts."""
    text = scene_body(scene)
    for dialogue in scene.dialogues:
        spoken = str(dialogue.text or "").strip()
        if spoken:
            text = re.sub(re.escape(spoken), " ", text, flags=re.IGNORECASE)
    return text


def _sentences(scene: Scene) -> list[str]:
    return [
        item.strip() for item in _SENTENCE_RE.split(_dialogue_stripped_body(scene)) if item.strip()
    ]


def sentence_scope(sentence: str) -> str:
    folded = key(sentence)
    raw = sentence.casefold()

    # Merely standing beside/looking at/pointing at a monitor is a real-world action.
    monitor_physical = any(
        marker in folded
        for marker in (
            "dung truoc man hinh",
            "nhin man hinh",
            "chi vao man hinh",
            "truoc man hinh camera",
            "tua doan video",
            "rewind the video",
            "standing in front of the monitor",
            "looking at the monitor",
            "points at the monitor",
        )
    ) or (
        "camera an ninh" in folded
        and not any(
            marker in folded
            for marker in (
                "trong doan video",
                "trong footage",
                "trong video",
                "tren man hinh co",
                "trên màn hình có",
            )
        )
    )
    if monitor_physical:
        return "physical_world"

    # "trong ảnh" must be matched with Vietnamese diacritics. Its accentless form
    # collides with ordinary prose such as "trống; anh".
    if "trong ảnh" in raw:
        return "photo"

    for marker, scope in _PERCEPTUAL_MARKERS:
        if key(marker) in folded or marker.casefold() in raw:
            return scope
    return "physical_world"


def _scoped_sentences(scene: Scene) -> list[tuple[str, str]]:
    """Carry nested-screen scope across follow-up sentences until source returns to reality."""
    output: list[tuple[str, str]] = []
    active_scope = "physical_world"
    carry = 0
    camera_context = 0
    for sentence in _sentences(scene):
        folded = key(sentence)
        structural_monitor = (
            "trong phong co" in folded and "man hinh camera an ninh" in folded
        ) or ("room has" in folded and "security monitor" in folded)
        physical_reset = structural_monitor or any(
            marker in folded
            for marker in (
                "hien tai",
                "current kh",
                "currently",
                "back in the room",
                "back in reality",
                "thuc tai",
                "tua doan video",
                "quay ve phia",
                "nhin bao",
                "bao chi vao",
            )
        )
        explicit = sentence_scope(sentence)
        mentions_camera_system = any(
            marker in folded for marker in ("camera an ninh", "security camera", "cctv")
        )
        if mentions_camera_system and explicit == "physical_world":
            camera_context = 2

        if physical_reset:
            scope = "physical_world"
            active_scope = "physical_world"
            carry = 0
        elif explicit != "physical_world":
            if explicit == "screen" and (
                (active_scope == "cctv" and carry > 0) or camera_context > 0
            ):
                scope = "cctv"
                active_scope = "cctv"
            else:
                scope = explicit
                active_scope = explicit
            carry = 2
        elif active_scope != "physical_world" and carry > 0:
            scope = active_scope
            carry -= 1
        else:
            scope = "physical_world"
            active_scope = "physical_world"
            carry = 0
        output.append((sentence, scope))
        if camera_context > 0 and not mentions_camera_system:
            camera_context -= 1
    return output


def physical_source_text(scene: Scene) -> str:
    """Remove nested footage/photo/screen facts from the physical-world evidence."""
    return " ".join(
        sentence for sentence, scope in _scoped_sentences(scene) if scope == "physical_world"
    )


def perceptual_entity_scopes(
    scene: Scene,
    *,
    characters: list[Character],
    props: list[Prop],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for sentence, scope in _scoped_sentences(scene):
        if scope == "physical_world":
            continue
        folded = key(sentence)
        for entity in [*characters, *props]:
            name_key = key(entity.name)
            aliases = [name_key]
            if isinstance(entity, Prop):
                aliases = _prop_aliases(entity)
            if any(
                alias and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded)
                for alias in aliases
            ):
                result[entity.id] = scope
    return result


def temporal_state(scene: Scene) -> SceneTemporalState:
    context = scene_context(scene)
    folded_context = key(context)
    if "flashback" in folded_context:
        branch = "flashback"
    elif "song song" in folded_context or "parallel" in folded_context:
        branch = "parallel"
    else:
        branch = "present"

    daypart = "source-defined time"
    for marker, label in _DAYPARTS:
        if key(marker) in folded_context:
            daypart = label
            break

    # A scene clock is authoritative only when present in the scene heading/context or
    # explicitly phrased as current scene time. Watch/ticket/display values are observations.
    scene_clock = ""
    context_clock = _CLOCK_RE.search(context)
    if context_clock:
        scene_clock = context_clock.group(1)
    else:
        body = scene_body(scene)
        explicit = re.search(
            r"(?:\blúc\b|\bluc\b|\bbây giờ là\b|\bbay gio la\b|"
            r"\bcurrent time(?: is)?\b|\bat\s+)(?:\s*)(\d{1,2}:\d{2})",
            key(body),
            re.IGNORECASE,
        )
        if explicit:
            scene_clock = explicit.group(1)

    observations: dict[str, str] = {}
    active_label = ""
    carry_label = 0
    for sentence in _sentences(scene):
        folded = key(sentence)
        explicit_label = ""
        if any(token in folded for token in ("dong ho", "watch", "clock")):
            explicit_label = "watch"
        elif any(
            token in folded for token in ("ve tau", "ticket", "khoi hanh", "departure", "tren ve")
        ):
            explicit_label = "ticket"
        elif any(token in folded for token in ("camera", "cctv", "video", "man hinh")):
            explicit_label = "screen"
        if explicit_label:
            active_label = explicit_label
            carry_label = 1
        clocks = list(_CLOCK_RE.finditer(sentence))
        for match in clocks:
            label = explicit_label or (active_label if carry_label > 0 else "") or "display"
            observations.setdefault(label, match.group(1))
        if not explicit_label:
            if carry_label > 0:
                carry_label -= 1
            elif not clocks:
                active_label = ""

    return SceneTemporalState(
        timeline_branch=branch,
        daypart=daypart,
        scene_clock=scene_clock,
        diegetic_clock_observations=observations,
    )


def _prop_aliases(prop: Prop) -> list[str]:
    folded = key(prop.name)
    aliases = {folded}
    tokens = [t for t in folded.split() if t not in {"chiec", "cai", "mau", "giay", "nho"}]
    if len(tokens) >= 2:
        aliases.add(" ".join(tokens))
    if "ve tau" in folded or "ticket" in folded:
        # Never use bare "ve": Vietnamese "vé" and "về" normalize to the same token.
        aliases.update(
            {
                "chiec ve",
                "ve tau",
                "ve xanh",
                "ve giay",
                "ticket",
                "train ticket",
            }
        )
    if "may ghi am" in folded or "recorder" in folded:
        aliases.update({"may ghi am", "recorder"})
    if "khan quang" in folded or "scarf" in folded:
        aliases.update({"khan quang", "khan do", "khan quang do", "scarf", "red scarf"})
    if "chia khoa" in folded or "key" in folded:
        aliases.update({"chia khoa", "chia khoa dong", "key"})
        numbers = re.findall(r"\b\d+\b", folded)
        for number in numbers:
            aliases.update(
                {
                    f"chia khoa so {number}",
                    f"chia khoa dong so {number}",
                    f"chia khoa {number}",
                    f"key {number}",
                }
            )
    if "o mau vang" in folded or folded.startswith("o ") or "umbrella" in folded:
        aliases.update({"o vang", "chiec o", "umbrella"})
    if "dong ho" in folded or "watch" in folded or "clock" in folded:
        aliases.update({"dong ho", "watch", "clock"})
    if "dien thoai" in folded or "phone" in folded or "telephone" in folded:
        aliases.update({"dien thoai", "phone", "telephone"})
    return sorted((x for x in aliases if x), key=len, reverse=True)


def _prop_in_text(text: str, prop: Prop) -> bool:
    raw = str(text or "").casefold()
    folded = key(text)
    prop_key = key(prop.name)
    if "ve tau" in prop_key or "ticket" in prop_key:
        # Preserve Vietnamese diacritics here: "vé" must never collide with "về".
        # Also remove place/function compounds such as "quầy vé" before treating bare
        # "vé" as a physical ticket mention.
        ticket_raw = re.sub(
            r"(?<!\w)(?:quầy|phòng|cửa|máy\s+bán|quầy\s+bán)\s+vé(?!\w)",
            " ",
            raw,
        )
        if re.search(r"(?<!\w)vé(?!\w)", ticket_raw):
            return True
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded)
        for alias in _prop_aliases(prop)
    )


def prop_mentioned_in_text(text: str, prop: Prop) -> bool:
    return _prop_in_text(text, prop)


def _prop_sentences(scene: Scene, prop: Prop) -> list[str]:
    return [
        sentence
        for sentence, scope in _scoped_sentences(scene)
        if scope == "physical_world" and _prop_in_text(sentence, prop)
    ]


_NEGATIVE_PROP_MARKERS = (
    "khong cam",
    "khong giu",
    "khong mang",
    "khong co o day",
    "khong co",
    "chua co",
    "not holding",
    "does not hold",
    "doesnt hold",
    "not carrying",
    "does not carry",
    "doesnt carry",
    "is not here",
    "not here",
    "without",
)


def _negative_prop_sentence(sentence: str) -> bool:
    folded = key(sentence)
    return any(marker in folded for marker in _NEGATIVE_PROP_MARKERS)


def prop_explicitly_absent(scene: Scene, prop: Prop) -> bool:
    mentions = _prop_sentences(scene, prop)
    return bool(mentions) and all(_negative_prop_sentence(sentence) for sentence in mentions)


def prop_physically_mentioned(scene: Scene, prop: Prop) -> bool:
    return any(not _negative_prop_sentence(sentence) for sentence in _prop_sentences(scene, prop))


def _explicit_prop_part(text: str, prop: Prop) -> str:
    folded = key(text)
    prop_key = key(prop.name)
    if "ve tau" not in prop_key and "ticket" not in prop_key:
        return "whole"
    if not _prop_in_text(text, prop):
        return "whole"

    right_corner = bool(
        re.search(r"\bmanh\b.{0,40}\bgoc phai\b.{0,60}\bve\b", folded)
        or re.search(r"\bright corner fragment\b", folded)
        or re.search(r"\bfragment\b.{0,40}\bright corner\b", folded)
    )
    if right_corner:
        return "right_corner_fragment"

    plural_main_pieces = bool(
        re.search(r"\b(?:hai|2)\s+manh\b", folded)
        or re.search(r"\b(?:two|2)\s+(?:pieces|fragments)\b", folded)
    )
    generic_fragment = (not plural_main_pieces) and bool(
        re.search(r"\bmanh\b.{0,60}\bve\b", folded)
        or re.search(r"\bfragment\b.{0,60}\bticket\b", folded)
    )
    if generic_fragment:
        return "fragment"
    return "whole"


def _positive_prop_sentences(scene: Scene, prop: Prop) -> list[str]:
    return [
        sentence
        for sentence, scope in _scoped_sentences(scene)
        if (
            scope == "physical_world"
            and _prop_in_text(sentence, prop)
            and not _negative_prop_sentence(sentence)
        )
    ]


def _fragment_mentions(scene: Scene, prop: Prop) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for sentence in _positive_prop_sentences(scene, prop):
        part = _explicit_prop_part(sentence, prop)
        if part != "whole":
            result.append((sentence, part))

    # Some screenplays establish the physical fragment in one sentence and identify
    # its parent object in the immediately following sentence, e.g.
    # "một mảnh giấy nhỏ. Cận cảnh: GÓC PHẢI CỦA CHIẾC VÉ XANH."
    # Allow only adjacent physical sentences to join so unrelated mentions cannot merge.
    prop_key = key(prop.name)
    if "ve tau" in prop_key or "ticket" in prop_key:
        scoped = [
            sentence for sentence, scope in _scoped_sentences(scene) if scope == "physical_world"
        ]
        for left, right in zip(scoped, scoped[1:], strict=False):
            left_key = key(left)
            right_key = key(right)
            left_is_fragment = any(
                marker in left_key for marker in ("manh giay", "paper fragment", "small fragment")
            )
            right_identifies_corner = _prop_in_text(right, prop) and "goc phai" in right_key
            if left_is_fragment and right_identifies_corner:
                result.append((f"{left} {right}", "right_corner_fragment"))

    if any(part == "right_corner_fragment" for _sentence, part in result):
        result = [
            (
                sentence,
                "right_corner_fragment" if part == "fragment" else part,
            )
            for sentence, part in result
        ]
    return result


def prop_whole_physically_mentioned(scene: Scene, prop: Prop) -> bool:
    fragment_mentions = _fragment_mentions(scene, prop)
    fragment_evidence = [evidence for evidence, _part in fragment_mentions]
    return any(
        _explicit_prop_part(sentence, prop) == "whole"
        and not any(sentence in evidence for evidence in fragment_evidence)
        for sentence in _positive_prop_sentences(scene, prop)
    )


def prop_part_from_source(scene: Scene, prop: Prop) -> str:
    mentions = _fragment_mentions(scene, prop)
    if any(part == "right_corner_fragment" for _sentence, part in mentions):
        return "right_corner_fragment"
    if mentions:
        return "fragment"
    return "whole"


def _part_instance_id(prop_id: str, part: str) -> str:
    return f"{prop_id}::{part}"


def _part_state_from_evidence(
    scene: Scene,
    prop: Prop,
    *,
    part: str,
    evidence: str,
    characters: list[Character],
) -> PropPhysicalState:
    owner = _infer_owner(
        evidence,
        prop,
        [character for character in characters if character.id in scene.characters],
    )
    location_id = _surface_location(evidence, scene.location_id)
    if location_id != scene.location_id and not any(
        marker in key(evidence)
        for marker in ("cam", "giu", "deo", "tren co", "hold", "holds", "carrying")
    ):
        owner = ""
    instance_id = _part_instance_id(prop.id, part)
    return PropPhysicalState(
        entity_id=prop.id,
        instance_id=instance_id,
        part=part,
        owner_id=owner,
        location_id=location_id,
        condition="fragment",
        piece_count=1,
        visibility="visible",
        scope="physical_world",
    )


def _surface_location(text: str, scene_location_id: str) -> str:
    folded = key(text)
    mappings = (
        (("ngan keo", "drawer"), "drawer"),
        (("khe duoi cua", "duoi cua", "under the door"), "door"),
        (("duoi san", "tren san", "on the floor"), "floor"),
        (
            (
                "tren quay",
                "len quay",
                "quay ve",
                "ticket counter",
                "on the counter",
                "onto the counter",
            ),
            "counter",
        ),
        (
            ("tren ban", "len ban", "mat ban", "on the table", "onto the table"),
            "table",
        ),
        (
            (
                "tren ghe",
                "tren chiec ghe",
                "ghe kim loai",
                "lung ghe",
                "tren lung ghe",
                "on the chair",
                "on the bench",
                "chair back",
            ),
            "seat",
        ),
        (("canh cua", "ben cua", "by the door", "beside the door"), "door"),
    )
    for markers, suffix in mappings:
        if any(marker in folded for marker in markers):
            return f"{scene_location_id}:{suffix}"
    return scene_location_id


def _character_occurrences(text: str, characters: list[Character]) -> list[tuple[int, str]]:
    folded = key(text)
    values: list[tuple[int, str]] = []
    for item in characters:
        needle = key(item.name)
        if not needle:
            continue
        match = re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", folded)
        if match:
            values.append((match.start(), item.id))
    return sorted(values)


def _infer_owner(text: str, prop: Prop, characters: list[Character]) -> str:
    """Infer physical ownership from sentence-local evidence only."""
    aliases = _prop_aliases(prop)
    positive_sentences: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", str(text or "")):
        if not sentence.strip() or not _prop_in_text(sentence, prop):
            continue
        folded_sentence = key(sentence)
        if any(
            marker in folded_sentence
            for marker in (
                "chua co",
                "khong co",
                "khong cam",
                "khong giu",
                "khong mang",
                "not have",
                "does not have",
                "doesnt have",
                "not holding",
                "without",
            )
        ):
            continue
        positive_sentences.append(sentence)

    for sentence in positive_sentences:
        folded = key(sentence)
        for character in characters:
            char_key = key(character.name)
            if not char_key:
                continue
            for alias in aliases:
                holds = re.search(
                    rf"(?<![a-z0-9]){re.escape(char_key)}(?![a-z0-9]).{{0,56}}"
                    rf"\b(?:cam|giu|lay|hold|holds|take|takes|carrying|has)\b.{{0,56}}"
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                    folded,
                )
                worn = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9]).{{0,48}}"
                    rf"\b(?:tren co|quang tren co|deo|worn by|around neck)\b.{{0,48}}"
                    rf"(?<![a-z0-9]){re.escape(char_key)}(?![a-z0-9])",
                    folded,
                )
                if holds or worn:
                    return character.id

        positions = _character_occurrences(sentence, characters)
        prop_pos = min(
            (folded.find(alias) for alias in aliases if folded.find(alias) >= 0),
            default=-1,
        )
        if prop_pos >= 0:
            before = [item for item in positions if item[0] <= prop_pos]
            if before:
                return before[-1][1]

    if len(characters) == 1 and positive_sentences:
        return characters[0].id
    return ""


def _explicit_condition_from_text(
    text: str,
    prop: Prop,
) -> tuple[str, int] | None:
    folded = key(text)
    if not _prop_in_text(text, prop):
        return None
    prop_key = key(prop.name)
    is_ticket = "ve tau" in prop_key or "ticket" in prop_key
    if is_ticket and (
        re.search(r"\bxe\b.{0,48}\b(?:lam doi|thanh hai)\b", folded)
        or re.search(
            r"\b(?:tear|tears|rip|rips)\b.{0,48}\b(?:in half|in two|apart)\b",
            folded,
        )
        or any(phrase in folded for phrase in ("torn in half", "ripped in two"))
    ):
        return "torn_in_two", 2
    if (
        is_ticket
        and ("goc phai" in folded or "right corner" in folded)
        and any(
            token in folded
            for token in (
                "rach",
                "xé",
                "xe",
                "torn",
                "missing",
                "thieu",
                "bi thieu",
                "mat goc",
                "missing corner",
            )
        )
    ):
        return "missing_right_corner", 1
    if any(
        marker in folded
        for marker in ("nguyen ven", "con nguyen", "intact", "undamaged", "whole ticket")
    ):
        return "intact", 1
    return None


def _condition_from_text(text: str, prop: Prop) -> tuple[str, int]:
    return _explicit_condition_from_text(text, prop) or ("intact", 1)


_PROP_EVENT_CUES = (
    "xe",
    "rach",
    "goc",
    "corner",
    "torn",
    "missing",
    "tear",
    "rip",
    "dua",
    "trao",
    "dat",
    "give",
    "hand",
    "place",
    "lay",
    "nhat",
    "take",
    "cat",
    "put away",
    "roi",
    "drop",
    "bat",
    "activate",
)


def _event_evidence(scene: Scene, prop: Prop, props: list[Prop]) -> list[str]:
    """Return physical event sentences, carrying one unambiguous prop across pronoun action."""
    result: list[str] = []
    scoped = _scoped_sentences(scene)
    explicit_scene_props = {
        item.id
        for item in props
        if any(
            scope == "physical_world" and _prop_in_text(sentence, item)
            for sentence, scope in scoped
        )
    }
    active_prop_id = ""
    carry = 0
    for sentence, scope in scoped:
        if scope != "physical_world":
            active_prop_id = ""
            carry = 0
            continue
        mentioned = [item.id for item in props if _prop_in_text(sentence, item)]
        if prop.id in mentioned:
            result.append(sentence)
            active_prop_id = prop.id
            carry = 1
            continue
        if mentioned:
            active_prop_id = mentioned[-1]
            carry = 1
            continue
        folded = key(sentence)
        pronoun_action = any(cue in folded for cue in _PROP_EVENT_CUES)
        if (
            not mentioned
            and not active_prop_id
            and len(explicit_scene_props) == 1
            and prop.id in explicit_scene_props
            and pronoun_action
        ):
            result.append(sentence)
            active_prop_id = prop.id
            carry = 1
            continue
        if active_prop_id == prop.id and carry > 0 and pronoun_action:
            result.append(sentence)
            carry -= 1
            continue
        if carry > 0:
            carry -= 1
        else:
            active_prop_id = ""
    return result


def _strip_quoted_payload(text: str) -> str:
    """Remove quoted message/dialogue payload while preserving surrounding physical action."""
    return re.sub(r"[“\"][^”\"]*[”\"]", " ", str(text or ""))


def _explicit_owner_claim(
    text: str,
    prop: Prop,
    characters: list[Character],
) -> str:
    folded = key(text)
    for character in characters:
        char_key = key(character.name)
        if not char_key:
            continue
        for alias in _prop_aliases(prop):
            pattern = (
                rf"(?<![a-z0-9]){re.escape(char_key)}(?![a-z0-9]).{{0,48}}"
                rf"(?:cam|giu|hold|holds|holding|carrying).{{0,48}}"
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            )
            if re.search(pattern, folded):
                return character.id
    return ""


def _negated_transfer(text: str) -> bool:
    folded = key(text)
    return any(
        marker in folded
        for marker in (
            "khong dua",
            "khong trao",
            "khong dat",
            "khong giao",
            "does not give",
            "doesnt give",
            "did not give",
            "didnt give",
            "does not hand",
            "did not hand",
            "never gives",
            "never hands",
        )
    )


def _event_action_offset(event: PropEvent) -> int:
    raw = event.evidence.casefold()
    folded = key(event.evidence)
    markers: dict[str, tuple[str, ...]] = {
        "pick_up": ("nhặt", "pick up", "picked up", "cầm"),
        "take_out": ("lấy", "take", "takes"),
        "place": ("đặt", "place", "put"),
        "transfer": ("đưa", "trao", "trả", "giao", "đặt", "give", "hand"),
        "tear": ("xé", "tear", "rip"),
        "put_away": ("cất", "bỏ", "put away", "tuck"),
        "drop": ("rơi", "drop", "fall"),
        "destroy": ("phá", "destroy"),
        "inspect": ("nhìn", "inspect"),
        "activate": ("bật", "activate", "turn on"),
    }
    offsets: list[int] = []
    for marker in markers.get(event.action, ()):
        pos = raw.find(marker)
        if pos < 0:
            pos = folded.find(key(marker))
        if pos >= 0:
            offsets.append(pos)
    return min(offsets) if offsets else 10_000


def _sort_prop_events(scene: Scene, events: list[PropEvent]) -> list[PropEvent]:
    source = physical_source_text(scene)
    action_priority = {
        "pick_up": 10,
        "take_out": 10,
        "place": 20,
        "tear": 30,
        "transfer": 40,
        "put_away": 50,
        "drop": 60,
        "destroy": 70,
    }

    def sort_key(event: PropEvent) -> tuple[int, int, int]:
        sentence_offset = source.find(event.evidence)
        if sentence_offset < 0:
            sentence_offset = 10_000
        return (
            sentence_offset,
            _event_action_offset(event),
            action_priority.get(event.action, 100),
        )

    return sorted(events, key=sort_key)


def extract_prop_events(
    scene: Scene,
    *,
    props: list[Prop],
    characters: list[Character],
) -> list[PropEvent]:
    events: list[PropEvent] = []
    scene_characters = [item for item in characters if item.id in set(scene.characters)]
    for prop in props:
        for sentence in _event_evidence(scene, prop, props):
            action_text = _strip_quoted_payload(sentence)
            raw_sentence = action_text.casefold()
            folded = key(action_text)
            owner = _infer_owner(action_text, prop, scene_characters)
            char_hits = _character_occurrences(action_text, scene_characters)

            tears_in_two = (
                re.search(r"\bxe\b.{0,48}\b(?:lam doi|thanh hai)\b", folded)
                or re.search(
                    r"\b(?:tear|tears|rip|rips)\b.{0,48}\b(?:in half|in two|apart)\b",
                    folded,
                )
                or "torn in two" in folded
            )
            if tears_in_two:
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="tear",
                        actor_id=owner,
                        source_condition="intact_or_prior_condition",
                        target_condition="torn_in_two",
                        evidence=sentence,
                    )
                )
            elif "xe" in folded and any(x in folded for x in ("goc", "corner")):
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="tear",
                        actor_id=owner,
                        source_condition="intact",
                        target_condition="missing_right_corner",
                        evidence=sentence,
                    )
                )

            picked_up = (
                "nhặt" in raw_sentence
                or (
                    "cầm" in raw_sentence
                    and re.search(r"\blen\b", folded)
                    and _prop_in_text(action_text, prop)
                )
                or any(marker in folded for marker in ("pick up", "picks up", "picked up"))
            )
            if picked_up:
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="pick_up",
                        actor_id=owner,
                        target_owner_id=owner,
                        source_location=_surface_location(sentence, scene.location_id),
                        target_location=scene.location_id,
                        evidence=sentence,
                    )
                )

            take_out = ("lấy" in raw_sentence or any(x in folded for x in ("take", "takes"))) and (
                any(
                    x in folded
                    for x in (
                        "ra khoi tui",
                        "ra tu tui",
                        "tu tui",
                        "out of pocket",
                        "from pocket",
                    )
                )
                or (
                    "lấy" in raw_sentence
                    and re.search(r"\bra\b", folded)
                    and _prop_in_text(action_text, prop)
                )
            )
            if take_out:
                from_pocket = any(
                    marker in folded
                    for marker in (
                        "ra khoi tui",
                        "ra tu tui",
                        "tu tui",
                        "out of pocket",
                        "from pocket",
                    )
                )
                source_location = (
                    "pocket" if from_pocket else _surface_location(sentence, scene.location_id)
                )
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="take_out",
                        actor_id=owner,
                        target_owner_id=owner,
                        source_location=source_location,
                        target_location=scene.location_id,
                        evidence=sentence,
                    )
                )

            put_away = (
                ("cất" in raw_sentence or "bỏ" in raw_sentence)
                or any(x in folded for x in ("put away", "puts away", "tuck", "tucks"))
            ) and any(x in folded for x in ("vao tui", "into pocket", "in pocket"))
            if put_away:
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="put_away",
                        actor_id=owner,
                        target_owner_id=owner,
                        target_location="pocket",
                        evidence=sentence,
                    )
                )

            surface = _surface_location(sentence, scene.location_id)
            placed = (
                surface != scene.location_id
                and (
                    "đặt" in raw_sentence
                    or any(x in folded for x in ("place", "places", "put", "puts"))
                )
                and "đặt tay" not in raw_sentence
            )
            if placed:
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="place",
                        actor_id=owner,
                        source_owner_id=owner,
                        target_location=surface,
                        evidence=sentence,
                    )
                )

            transfer = (
                not _negated_transfer(sentence)
                and (
                    any(marker in raw_sentence for marker in ("đưa", "trao", "trả", "giao", "đặt"))
                    or any(
                        x in folded for x in ("give", "gives", "hand", "hands", "place", "places")
                    )
                )
                and any(
                    x in folded
                    for x in (
                        "cho",
                        "vao tay",
                        "trong tay",
                        "long ban tay",
                        "to ",
                        "into the palm",
                        "in the palm",
                        "into hand",
                        "in hand",
                    )
                )
            )
            if transfer and char_hits:
                target = char_hits[-1][1]
                source = next(
                    (
                        character_id
                        for _position, character_id in char_hits
                        if character_id != target
                    ),
                    owner,
                )
                if source == target and len(scene_characters) == 2:
                    source = next(
                        (item.id for item in scene_characters if item.id != target),
                        "",
                    )
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="transfer",
                        actor_id=source,
                        source_owner_id=source,
                        target_owner_id=target,
                        evidence=sentence,
                    )
                )

            if any(
                x in folded
                for x in ("roi xuong san", "rơi xuống sàn", "falls to the floor", "drop", "drops")
            ):
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="drop",
                        actor_id=owner,
                        source_owner_id=owner,
                        target_location=f"{scene.location_id}:floor",
                        evidence=sentence,
                    )
                )

            if any(x in folded for x in ("bat", "sang", "activate", "turns on")) and any(
                x in folded for x in ("led", "den", "light")
            ):
                events.append(
                    PropEvent(
                        entity_id=prop.id,
                        action="activate",
                        actor_id=owner,
                        evidence=sentence,
                    )
                )
    return events


def _base_prop_state(
    scene: Scene,
    prop: Prop,
    *,
    characters: list[Character],
    evidence_text: str,
) -> PropPhysicalState:
    condition, pieces = _condition_from_text(evidence_text, prop)
    part = _explicit_prop_part(evidence_text, prop)
    if part == "right_corner_fragment":
        condition = "fragment"
        pieces = 1
    owner = _infer_owner(evidence_text, prop, [c for c in characters if c.id in scene.characters])
    location_id = _surface_location(evidence_text, scene.location_id)
    if location_id != scene.location_id and not any(
        marker in key(evidence_text) for marker in ("cam", "giu", "hold", "holds", "carrying")
    ):
        owner = ""
    return PropPhysicalState(
        entity_id=prop.id,
        part=part,
        owner_id=owner,
        location_id=location_id,
        condition=condition,
        piece_count=pieces,
        visibility="visible",
        scope="physical_world",
    )


def _apply_event(state: PropPhysicalState, event: PropEvent) -> PropPhysicalState:
    new = state.model_copy(deep=True)
    if event.action == "tear":
        if event.target_condition == "torn_in_two":
            new.condition = "torn_in_two"
            new.piece_count = 2
        elif event.target_condition:
            new.condition = event.target_condition
    elif event.action == "pick_up":
        new.owner_id = event.target_owner_id
        new.location_id = event.target_location or new.location_id
        new.container = ""
        new.visibility = "visible"
    elif event.action == "place":
        new.owner_id = ""
        new.location_id = event.target_location or new.location_id
        new.container = ""
        new.visibility = "visible"
    elif event.action == "transfer":
        new.owner_id = event.target_owner_id
        new.container = ""
        new.visibility = "visible"
    elif event.action == "take_out":
        if event.target_owner_id:
            new.owner_id = event.target_owner_id
        if event.target_location:
            new.location_id = event.target_location
        new.container = ""
        new.visibility = "visible"
    elif event.action == "put_away":
        if event.target_owner_id:
            new.owner_id = event.target_owner_id
        new.container = event.target_location or "pocket"
        new.visibility = "offscreen"
    elif event.action == "drop":
        new.owner_id = ""
        new.container = ""
        new.location_id = event.target_location
        new.visibility = "visible"
    elif event.action == "destroy":
        new.present = False
        new.visibility = "offscreen"
    return new


def compile_prop_lifecycle(
    scene: Scene,
    *,
    props: list[Prop],
    characters: list[Character],
    previous_scene: Scene | None,
    direct_continuation: bool,
    prior_props: dict[str, PropPhysicalState] | None = None,
) -> tuple[
    dict[str, PropPhysicalState],
    dict[str, PropPhysicalState],
    dict[str, PropPhysicalState],
    dict[str, PropPhysicalState],
    list[PropEvent],
]:
    physical = physical_source_text(scene)
    events = extract_prop_events(scene, props=props, characters=characters)
    by_prop_events: dict[str, list[PropEvent]] = {}
    for event in events:
        by_prop_events.setdefault(event.entity_id, []).append(event)

    entry: dict[str, PropPhysicalState] = {}
    entry_parts: dict[str, PropPhysicalState] = {}
    if direct_continuation and previous_scene is not None:
        entry.update(
            {
                key: value.model_copy(deep=True)
                for key, value in previous_scene.semantic_truth.exit_props.items()
            }
        )
        entry_parts.update(
            {
                key: value.model_copy(deep=True)
                for key, value in previous_scene.semantic_truth.exit_part_instances.items()
            }
        )

    for prop in props:
        fragment_mentions = _fragment_mentions(scene, prop)
        whole_mentioned = prop_whole_physically_mentioned(scene, prop)

        if prop_explicitly_absent(scene, prop) and not fragment_mentions:
            entry.pop(prop.id, None)
            continue

        # A fragment-only scene must never masquerade as the canonical whole prop.
        if fragment_mentions and not whole_mentioned:
            entry.pop(prop.id, None)
            for evidence_sentence, part in fragment_mentions:
                instance_id = _part_instance_id(prop.id, part)
                candidate = _part_state_from_evidence(
                    scene,
                    prop,
                    part=part,
                    evidence=evidence_sentence,
                    characters=characters,
                )
                existing = entry_parts.get(instance_id)
                if existing is None:
                    entry_parts[instance_id] = candidate
                    continue
                if (
                    existing.location_id == scene.location_id
                    and candidate.location_id != scene.location_id
                ):
                    existing.location_id = candidate.location_id
                if (
                    not existing.owner_id
                    and candidate.owner_id
                    and existing.location_id == scene.location_id
                ):
                    existing.owner_id = candidate.owner_id
            continue

        if not whole_mentioned:
            continue

        evidence = " ".join(_event_evidence(scene, prop, props)) or physical
        prop_events = _sort_prop_events(
            scene,
            by_prop_events.get(prop.id, []),
        )
        by_prop_events[prop.id] = prop_events
        first_event = prop_events[0] if prop_events else None
        if first_event is not None and first_event.evidence in evidence:
            sentence_start = evidence.find(first_event.evidence)
            action_offset = _event_action_offset(first_event)
            if action_offset >= 10_000:
                action_offset = 0

            if first_event.action in {"pick_up", "take_out"}:
                # Acquisition sentences often encode the pre-action condition after the
                # verb ("lấy vé bị thiếu góc phải ... rồi xé"). Keep that descriptor,
                # but stop before the next authored transformation/movement.
                raw_sentence = first_event.evidence.casefold()
                next_action = re.search(
                    r"\b(?:rồi|sau đó|then)\s+"
                    r"(?:xé|tear|rip|đưa|trao|trả|bỏ|cất|đặt|rơi|drop)\b",
                    raw_sentence,
                )
                cut = next_action.start() if next_action else len(first_event.evidence)
                pre_event_evidence = evidence[:sentence_start] + first_event.evidence[:cut]
            else:
                pre_event_evidence = (
                    evidence[:sentence_start] + first_event.evidence[:action_offset]
                )
        else:
            pre_event_evidence = evidence
        entry_evidence = pre_event_evidence.strip() or (evidence if first_event is None else "")

        state = entry.get(prop.id)
        seeded_from_prior = state is not None
        if state is None and prior_props and prop.id in prior_props:
            state = prior_props[prop.id].model_copy(deep=True)
            state.location_id = scene.location_id
            state.visibility = "visible"
            seeded_from_prior = True
        if state is None:
            state = _base_prop_state(
                scene,
                prop,
                characters=characters,
                evidence_text=entry_evidence or prop.name,
            )

        owner_claim = _explicit_owner_claim(
            entry_evidence,
            prop,
            [c for c in characters if c.id in scene.characters],
        )
        if owner_claim and not seeded_from_prior:
            state.owner_id = owner_claim
            state.location_id = scene.location_id
            state.container = ""

        explicit_condition = _explicit_condition_from_text(entry_evidence, prop)
        if explicit_condition is not None:
            state.condition, state.piece_count = explicit_condition
        elif not seeded_from_prior and first_event is not None:
            state.condition = "intact"
            state.piece_count = 1

        # Acquisition events define the state immediately before the action.
        if first_event is not None and first_event.action == "pick_up":
            state.owner_id = ""
            source_location = _surface_location(pre_event_evidence, scene.location_id)
            if source_location == scene.location_id and first_event.source_location:
                source_location = first_event.source_location
            state.location_id = source_location
            state.container = ""
            first_event.source_location = source_location
        elif first_event is not None and first_event.action == "take_out":
            source_location = first_event.source_location
            if source_location == "pocket":
                state.owner_id = first_event.target_owner_id or state.owner_id
                state.location_id = scene.location_id
                state.container = "pocket"
            elif source_location and source_location != scene.location_id:
                state.owner_id = ""
                state.location_id = source_location
                state.container = source_location.rsplit(":", 1)[-1]
            else:
                state.owner_id = first_event.target_owner_id or state.owner_id

        entry[prop.id] = state

        # Resolve pronoun actors and transfer sources by replaying the typed event stream.
        working = state.model_copy(deep=True)
        for event in prop_events:
            if event.action in {"place", "drop"} and not event.source_owner_id:
                event.source_owner_id = working.owner_id
                event.actor_id = event.actor_id or working.owner_id
            if event.action in {"pick_up", "take_out"}:
                if not event.target_owner_id:
                    event.target_owner_id = event.actor_id or working.owner_id
            if event.action == "transfer":
                if not event.source_owner_id or event.source_owner_id == event.target_owner_id:
                    if working.owner_id and working.owner_id != event.target_owner_id:
                        event.source_owner_id = working.owner_id
                        event.actor_id = event.actor_id or working.owner_id
                if not event.target_owner_id and event.actor_id != working.owner_id:
                    event.target_owner_id = event.actor_id
            if event.action == "put_away" and not event.target_owner_id:
                event.target_owner_id = working.owner_id or event.actor_id
            working = _apply_event(working, event)

    events = _sort_prop_events(scene, events)
    exit_parts = {key: value.model_copy(deep=True) for key, value in entry_parts.items()}

    # Spawn detachable part instances at the exact transformation point.
    for prop in props:
        prop_events = [event for event in events if event.entity_id == prop.id]
        if prop.id not in entry:
            continue
        working = entry[prop.id].model_copy(deep=True)
        for event in prop_events:
            if event.action == "tear" and event.target_condition == "missing_right_corner":
                part = "right_corner_fragment"
                instance_id = _part_instance_id(prop.id, part)
                exit_parts[instance_id] = PropPhysicalState(
                    entity_id=prop.id,
                    instance_id=instance_id,
                    part=part,
                    owner_id=working.owner_id,
                    location_id=working.location_id or scene.location_id,
                    condition="fragment",
                    piece_count=1,
                    visibility="visible",
                    scope="physical_world",
                )
            working = _apply_event(working, event)

        # Source evidence after the split may refine where/who keeps the fragment.
        for evidence_sentence, part in _fragment_mentions(scene, prop):
            instance_id = _part_instance_id(prop.id, part)
            if instance_id in exit_parts:
                refined = _part_state_from_evidence(
                    scene,
                    prop,
                    part=part,
                    evidence=evidence_sentence,
                    characters=characters,
                )
                existing = exit_parts[instance_id]
                if (
                    existing.location_id == scene.location_id
                    and refined.location_id != scene.location_id
                ):
                    existing.location_id = refined.location_id
                if (
                    not existing.owner_id
                    and refined.owner_id
                    and existing.location_id == scene.location_id
                ):
                    existing.owner_id = refined.owner_id

    exit_state = {key: value.model_copy(deep=True) for key, value in entry.items()}
    for event in events:
        state = exit_state.get(event.entity_id)
        if state is None:
            prop = next((item for item in props if item.id == event.entity_id), None)
            if prop is None:
                continue
            state = _base_prop_state(
                scene, prop, characters=characters, evidence_text=event.evidence
            )
        exit_state[event.entity_id] = _apply_event(state, event)

    return entry, exit_state, entry_parts, exit_parts, events


def audit_ai_semantic_proposal(scene: Scene) -> None:
    """Record AI/source agreement; deterministic truth remains authoritative."""
    proposal = scene.ai_semantic_proposal
    truth = scene.semantic_truth
    overrides: list[str] = []

    for fact in proposal.rejected_facts:
        overrides.append(
            f"rejected unsupported fact {fact.fact_type}:{fact.entity_id}:{fact.value}"
        )

    def agrees(fact, *, negative_bucket: bool) -> bool:
        entity_id = fact.entity_id
        states = [
            state
            for state in (
                truth.entry_props.get(entity_id),
                truth.exit_props.get(entity_id),
            )
            if state is not None
        ]
        states.extend(
            state
            for state in (
                *truth.entry_part_instances.values(),
                *truth.exit_part_instances.values(),
            )
            if state.entity_id == entity_id
        )
        present = bool(states)
        fact_type = fact.fact_type
        value = str(fact.value or "").strip()
        if fact_type == "presence":
            return (not present) if negative_bucket else present
        if fact_type == "absence":
            return not present
        if fact_type == "ownership":
            if negative_bucket:
                return all(state.owner_id != value for state in states)
            return any(state.owner_id == value for state in states)
        if fact_type == "location":
            if negative_bucket:
                return all(value not in state.location_id for state in states)
            return any(value in state.location_id for state in states)
        if fact_type == "condition":
            if negative_bucket:
                return all(state.condition != value for state in states)
            return any(state.condition == value for state in states)
        if fact_type == "part":
            if negative_bucket:
                return all(state.part != value for state in states)
            return any(state.part == value for state in states)
        if fact_type == "event":
            actions = {
                event.action
                for event in truth.prop_events
                if not entity_id or event.entity_id == entity_id
            }
            return (value not in actions) if negative_bucket else (value in actions)
        if fact_type == "scope":
            actual = truth.perceptual_entities.get(entity_id, "physical_world" if present else "")
            return (actual != value) if negative_bucket else (actual == value)
        if fact_type == "time":
            temporal_values = {
                truth.temporal.timeline_branch,
                truth.temporal.daypart,
                truth.temporal.scene_clock,
                *truth.temporal.diegetic_clock_observations.values(),
            }
            return (value not in temporal_values) if negative_bucket else (value in temporal_values)
        return True

    for fact in proposal.facts:
        if not agrees(fact, negative_bucket=False):
            overrides.append(
                f"deterministic override {fact.fact_type}:{fact.entity_id}:{fact.value}"
            )
    for fact in proposal.negative_facts:
        if not agrees(fact, negative_bucket=True):
            overrides.append(
                f"deterministic override negative {fact.fact_type}:{fact.entity_id}:{fact.value}"
            )

    truth.source_trace["ai_semantic_audit"] = (
        f"accepted={len(proposal.facts) + len(proposal.negative_facts)};"
        f"rejected={len(proposal.rejected_facts)};"
        f"normalization_issues={len(proposal.normalization_issues)};"
        f"uncertainties={len(proposal.uncertainties)};"
        f"overrides={len(overrides)}"
    )
    if proposal.normalization_issues:
        truth.source_trace["ai_normalization_issues"] = " | ".join(proposal.normalization_issues)
    else:
        truth.source_trace.pop("ai_normalization_issues", None)
    if overrides:
        truth.source_trace["ai_overrides"] = " | ".join(overrides)
    elif "ai_overrides" in truth.source_trace:
        truth.source_trace.pop("ai_overrides", None)


def legacy_prop_state(value: PropPhysicalState) -> str:
    if not value.present:
        return "Not physically present after an explicit source action"
    condition = {
        "intact": "intact",
        "missing_right_corner": "one full item with the right corner missing",
        "torn_in_two": "torn into two physical pieces",
    }.get(value.condition, value.condition)
    parts = [f"physical condition={condition}", f"part={value.part}"]
    if value.piece_count != 1:
        parts.append(f"piece_count={value.piece_count}")
    if value.owner_id:
        parts.append(f"owner={value.owner_id}")
    if value.container:
        parts.append(f"container={value.container}")
    if value.location_id:
        parts.append(f"location={value.location_id}")
    return "; ".join(parts)


def narrative_transition(previous_scene: Scene | None, scene: Scene) -> str:
    context = key(scene_context(scene))
    if previous_scene is None:
        return "opening"
    if "song song" in context or "parallel" in context:
        return "parallel"
    if "flashback" in context and "flashback" not in key(scene_context(previous_scene)):
        return "flashback"
    if any(x in context for x in ("tro lai hien tai", "return to present")):
        return "return_from_flashback"
    if any(x in context for x in ("lien tuc", "continuous")):
        if previous_scene.location_id != scene.location_id:
            return "location_transition"
        return "continuous"
    if previous_scene.location_id != scene.location_id:
        return "location_transition"
    return "cut"


def compile_scene_semantic_truth(
    scene: Scene,
    *,
    characters: list[Character],
    props: list[Prop],
    previous_scene: Scene | None,
    direct_continuation: bool,
    prior_props: dict[str, PropPhysicalState] | None = None,
) -> SceneSemanticTruth:
    entry, exit_state, entry_parts, exit_parts, events = compile_prop_lifecycle(
        scene,
        props=props,
        characters=characters,
        previous_scene=previous_scene,
        direct_continuation=direct_continuation,
        prior_props=prior_props,
    )
    transition = narrative_transition(previous_scene, scene)
    return SceneSemanticTruth(
        entry_props=entry,
        exit_props=exit_state,
        entry_part_instances=entry_parts,
        exit_part_instances=exit_parts,
        prop_events=events,
        temporal=temporal_state(scene),
        perceptual_entities=perceptual_entity_scopes(scene, characters=characters, props=props),
        narrative_transition=transition,
        frame_anchor="previous_final_frame" if direct_continuation else "canonical_master",
        source_trace={
            "scene_context": scene_context(scene),
            "physical_source": physical_source_text(scene),
        },
    )


_LOCATION_ALIAS_MODIFIERS = {"cu", "old", "moi", "new"}


def physical_location_key(name: str) -> str:
    tokens = [token for token in key(name).split() if token not in _LOCATION_ALIAS_MODIFIERS]
    return " ".join(tokens)


def canonicalize_location_aliases(
    locations: list[Location],
) -> tuple[list[Location], dict[str, str]]:
    canonical: list[Location] = []
    remap: dict[str, str] = {}
    by_key: dict[str, Location] = {}
    for location in locations:
        identity = physical_location_key(location.name)
        existing = by_key.get(identity)
        if existing is None:
            canonical.append(location)
            by_key[identity] = location
            remap[location.id] = location.id
            continue
        remap[location.id] = existing.id
        # Keep the richest stable architecture/spatial description.
        if len(location.spatial_anchors) > len(existing.spatial_anchors):
            existing.spatial_anchors = location.spatial_anchors
        if len(location.objects) > len(existing.objects):
            existing.objects = list(location.objects)
    return canonical, remap


_ANCHOR_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cua kinh", "glass door"), "fixed glass entrance/door"),
    (("cua ra", "exit door", "entrance"), "fixed entrance/exit axis"),
    (("cua so", "window"), "fixed window"),
    (("quay ve", "ticket counter"), "fixed ticket counter"),
    (("quay", "counter"), "fixed counter"),
    (("man hinh camera an ninh", "cctv monitor", "security monitor"), "fixed CCTV monitor station"),
    (("thang may", "elevator"), "fixed elevator enclosure/doors"),
    (("hanh lang", "corridor"), "corridor axis"),
    (("san ga", "platform"), "platform/track-facing axis"),
    (("mai hien", "awning", "canopy"), "fixed canopy/awning"),
    (("cau thang", "stair"), "fixed stair axis"),
    (("cong", "gate"), "fixed gate"),
)


def enrich_location_spatial_anchors(project):
    """Add only source-evidenced fixed topology to canonical location identity."""
    for location in project.locations:
        evidence = " ".join(
            physical_source_text(scene)
            for scene in project.scenes
            if scene.location_id == location.id
        )
        folded = key(evidence)
        anchors: list[str] = []
        for markers, label in _ANCHOR_RULES:
            if any(key(marker) in folded for marker in markers):
                anchors.append(label)
        if not anchors:
            continue
        unique = list(dict.fromkeys(anchors))
        location.spatial_anchors = (
            "SOURCE-GROUNDED FIXED TOPOLOGY: "
            + "; ".join(unique)
            + ". Preserve these anchors and their relative geometry across every Master/scene."
        )
    return project

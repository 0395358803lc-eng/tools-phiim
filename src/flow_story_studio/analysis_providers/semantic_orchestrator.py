"""Source-grounded semantic orchestration for cinematic production.

The screenplay is the authority for identity and scene presence. AI output may enrich
attributes, but it cannot create a second identity for a declared entity, turn dialogue-
only characters into visible cast, or attach one canonical prop's state to another prop.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from ..engines.analyzer import _declared_characters, _declared_props, _heading_locations
from ..models import Character, Location, Project, Prop, Scene

_OFFSCREEN_PHRASES = (
    "v.o.",
    "v.o",
    "o.s.",
    "o.s",
    "offscreen",
    "off screen",
    "voice over",
    "voiceover",
    "through the phone",
    "over the phone",
    "on the phone",
    "qua điện thoại",
    "qua dien thoai",
    "giọng qua điện thoại",
    "giong qua dien thoai",
    "giọng trong máy ghi âm",
    "giong trong may ghi am",
    "voice in the recorder",
    "recorded voice",
)

_ABSENCE_PHRASES = (
    "không có mặt",
    "khong co mat",
    "không ở đây",
    "khong o day",
    "không có ở",
    "khong co o",
    "not present",
    "not here",
    "not in the room",
    "not in the apartment",
    "not in the scene",
    "not on screen",
    "not onscreen",
)

_PROP_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "with",
    "của",
    "cua",
    "màu",
    "mau",
    "chiếc",
    "chiec",
    "cái",
    "cai",
    "giấy",
    "giay",
    "nhỏ",
    "nho",
}

_CAMERA_SINGLE = (
    "Medium single-subject shot with motivated eye-line and clean negative space",
    "Tight medium single-subject shot emphasizing the current physical action",
    "Three-quarter single-subject composition preserving spatial geography",
    "Profile single-subject shot with motivated foreground depth",
    "Over-shoulder-style single-subject framing without introducing another person",
    "Slow push-in on the single visible subject, motivated by the dramatic beat",
)
_CAMERA_EMPTY = (
    "Wide environmental establishing shot motivated by the current location beat",
    "Static architectural composition emphasizing the active environment and props",
    "Slow environmental push-in with no person visible in frame",
    "Detail-led environmental shot preserving location geography",
)
_CAMERA_MULTI = (
    "Balanced two-or-more-subject composition preserving screen direction",
    "Layered multi-subject medium-wide shot with clear foreground/background blocking",
    "Motivated over-shoulder coverage using only the declared visible cast",
    "Lateral multi-subject composition preserving eye-lines and spatial geography",
    "Wide ensemble composition using only the visible characters in this scene",
)


def semantic_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold().replace("đ", "d"))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def semantic_score(left: object, right: object) -> float:
    a = semantic_key(left)
    b = semantic_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.94
    at = set(a.split())
    bt = set(b.split())
    overlap = len(at & bt) / max(1, len(at | bt))
    return max(overlap, SequenceMatcher(None, a, b).ratio())


def _raw_word_match(text: str, name: str) -> bool:
    raw_text = str(text).casefold()
    raw_name = str(name).casefold().strip()
    tokens = re.findall(r"[\wÀ-ỹ]+", raw_name, re.UNICODE)
    if len(tokens) == 1 and len(tokens[0]) <= 3:
        return re.search(rf"(?<!\w){re.escape(tokens[0])}(?!\w)", raw_text, re.UNICODE) is not None
    key = semantic_key(name)
    folded = semantic_key(text)
    return bool(key and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", folded))


def _declared_match(name: str, declared: list[str]) -> bool:
    return any(semantic_score(name, item) >= 0.82 for item in declared)


def _choose_entity(name: str, items: list[Character] | list[Location] | list[Prop]):
    best = None
    score = 0.0
    for item in items:
        current = semantic_score(name, item.name)
        if current > score:
            best = item
            score = current
    return best if best is not None and score >= 0.72 else None


def source_canonical_world(
    project: Project,
    source_project: Project | None = None,
) -> tuple[list[Character], list[Location], list[Prop]]:
    """Build canonical world from deterministic source identities, not AI-renamed IDs."""
    source_project = source_project or project
    declared_characters = _declared_characters(project.original_text)
    declared_props = _declared_props(project.original_text)
    heading_locations = _heading_locations(project.original_text)

    def source_locked_overrides(source_item):
        if isinstance(source_item, Character):
            baseline = Character(id=source_item.id, name=source_item.name)
            return {
                field: value
                for field, value in source_item.model_dump().items()
                if field not in {"id", "name"} and value != getattr(baseline, field)
            }
        if isinstance(source_item, Prop):
            baseline = Prop(
                id=source_item.id,
                name=source_item.name,
                description=f"{source_item.name} xuất hiện trong nội dung gốc",
            )
            return {
                field: value
                for field, value in source_item.model_dump().items()
                if field not in {"id", "name"} and value != getattr(baseline, field)
            }
        return {}

    def enrich_source_items(source_items, ai_items, allowed_names=None):
        result = []
        seen = set()
        for source_item in source_items:
            if allowed_names and not any(
                semantic_score(source_item.name, name) >= 0.72 for name in allowed_names
            ):
                continue
            key = semantic_key(source_item.name)
            if key in seen:
                continue
            seen.add(key)
            enriched = _choose_entity(source_item.name, ai_items)
            if enriched is None:
                result.append(source_item)
                continue
            merged = enriched.model_dump()
            merged.update(source_locked_overrides(source_item))
            merged.update({"id": source_item.id, "name": source_item.name})
            result.append(type(source_item).model_validate(merged))
        return result

    characters = enrich_source_items(
        source_project.characters,
        project.characters,
        declared_characters or None,
    )
    props = enrich_source_items(
        source_project.props,
        project.props,
        declared_props or None,
    )
    # Scene headings are useful anchors but are not exhaustive. Keep all deterministic
    # source locations; their IDs/names are the identity manifest. AI may enrich only
    # when semantic identity still matches.
    locations = enrich_source_items(source_project.locations, project.locations)

    # If an explicit heading list exists, prefer source locations matching those headings
    # first while still retaining source-inferred locations from prose/flashbacks.
    if heading_locations:
        locations.sort(
            key=lambda item: (
                not any(semantic_score(item.name, name) >= 0.72 for name in heading_locations),
                item.id,
            )
        )

    return (
        characters or list(source_project.characters),
        locations or list(source_project.locations),
        props or list(source_project.props),
    )


def remap_to_world(old_items, canonical_items) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for old in old_items:
        match = _choose_entity(old.name, canonical_items)
        if match is not None:
            mapping[old.id] = match.id
    for item in canonical_items:
        mapping[item.id] = item.id
    return mapping


def _strip_scene_context(text: str) -> str:
    return re.sub(r"\[SCENE CONTEXT\].*?\[END CONTEXT\]", " ", str(text), flags=re.DOTALL)


def _name_occurrences(text: str, name: str) -> list[tuple[int, int]]:
    source = _strip_scene_context(text)
    raw_name = str(name).strip()
    tokens = re.findall(r"[\wÀ-ỹ]+", raw_name, re.UNICODE)
    if len(tokens) == 1 and len(tokens[0]) <= 3:
        # Short names are collision-prone in Vietnamese (AN vs. "an ninh").
        # Accept declared uppercase and title-case forms, but never arbitrary lowercase words.
        forms = {raw_name, raw_name.upper(), raw_name.title()}
        forms = {form for form in forms if form}
        pattern = (
            r"(?<!\w)(?:"
            + "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
            + r")(?!\w)"
        )
        return [(m.start(), m.end()) for m in re.finditer(pattern, source, re.UNICODE)]
    raw_text = source.casefold()
    raw_name_folded = raw_name.casefold()
    return [
        (m.start(), m.end())
        for m in re.finditer(rf"(?<!\w){re.escape(raw_name_folded)}(?!\w)", raw_text, re.UNICODE)
    ]


def _remote_dialogue_ranges(source: str) -> list[tuple[int, int]]:
    raw = source.casefold()
    ranges: list[tuple[int, int]] = []
    label_patterns = (
        (
            r"(?<!\w)[\wÀ-ỹ .'-]{1,40}\s*\("
            r"(?:qua\s+điện\s+thoại|qua\s+dien\s+thoai|v\.?o\.?|o\.?s\.?)\)"
        ),
        (
            r"(?:giọng|giong|voice)\s+[\wÀ-ỹ .'-]{1,32}\s+"
            r"(?:trong\s+máy\s+ghi\s+âm|trong\s+may\s+ghi\s+am|"
            r"in\s+(?:the\s+)?recorder|from\s+(?:the\s+)?recorder)"
        ),
    )
    for pattern in label_patterns:
        for match in re.finditer(pattern, raw, re.UNICODE):
            sentence_end = len(raw)
            punctuation = re.search(r"[.!?]", raw[match.end() :])
            if punctuation:
                sentence_end = match.end() + punctuation.end()
            ranges.append((match.start(), sentence_end))
    return ranges


def _communication_metadata_ranges(source: str, name: str) -> list[tuple[int, int]]:
    raw = _strip_scene_context(source)
    folded = semantic_key(raw)
    key = semantic_key(name)
    if not key:
        return []
    patterns = (
        rf"(?:man\s+hinh\s+hien|caller\s+id|incoming\s+call).{{0,24}}\b{re.escape(key)}\b",
        rf"\b{re.escape(key)}\b\s+(?:dang\s+goi|is\s+calling|calling)\b",
        rf"(?:tin\s+nhan\s+tu|message\s+from|text\s+from)\s+\b{re.escape(key)}\b",
    )
    ranges: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, folded):
            # Folded offsets do not map perfectly to Unicode source offsets, so mark
            # the whole event semantically and let character_presence short-circuit
            # matching by event text instead of exact offsets.
            ranges.append((match.start(), match.end()))
    return ranges


def _is_communication_only_occurrence(window: str, name: str) -> bool:
    folded = semantic_key(window)
    key = semantic_key(name)
    patterns = (
        rf"(?:man\s+hinh\s+hien|caller\s+id|incoming\s+call).{{0,24}}\b{re.escape(key)}\b",
        rf"\b{re.escape(key)}\b\s+(?:dang\s+goi|is\s+calling|calling)\b",
        rf"(?:tin\s+nhan\s+tu|message\s+from|text\s+from)\s+\b{re.escape(key)}\b",
    )
    return any(re.search(pattern, folded) for pattern in patterns)


def _mention_is_nonvisual(window: str, name: str) -> bool:
    folded = semantic_key(window)
    raw = window.casefold()
    escaped_raw = re.escape(name.casefold().strip())
    escaped_key = re.escape(semantic_key(name))

    negative_patterns = (
        rf"(?:không|khong)\s+(?:có|co)\s+{escaped_raw}\b",
        rf"\bno\s+{escaped_raw}\b",
        rf"\bwithout\s+{escaped_raw}\b",
        rf"{escaped_raw}\s+(?:is\s+not|isn't)\b",
        rf"{escaped_raw}\s+(?:không|khong)\s+(?:có\s+mặt|co\s+mat|ở|o|xuất\s+hiện|xuat\s+hien)\b",
    )
    if any(re.search(pattern, raw, re.UNICODE) for pattern in negative_patterns):
        return True

    nonvisual_patterns = (
        rf"{escaped_key}\s+(?:chi\s+ton\s+tai|only\s+exists)\s+(?:trong|in)\b",
        rf"(?:cua|of)\s+{escaped_key}\b",
        rf"{escaped_key}(?:\s+s)?\s+voice\s+(?:through|over|on|from)\s+(?:the\s+)?phone\b",
        rf"voice\s+of\s+{escaped_key}\b",
        (
            rf"(?:giong|voice)\s+(?:cua\s+|of\s+)?{escaped_key}\b.{{0,30}}"
            r"(?:dien\s+thoai|phone|recorder|may\s+ghi\s+am)"
        ),
        (
            rf"{escaped_key}\b.{{0,12}}"
            r"(?:qua\s+dien\s+thoai|through\s+(?:the\s+)?phone|"
            r"over\s+(?:the\s+)?phone)"
        ),
        rf"(?:man\s+hinh\s+hien|caller\s+id|incoming\s+call).{0, 12}{escaped_key}\b",
        rf"{escaped_key}\s+(?:dang\s+goi|is\s+calling|calling)\b",
        rf"(?:tin\s+nhan\s+tu|message\s+from|text\s+from)\s+{escaped_key}\b",
    )
    return any(re.search(pattern, folded) for pattern in nonvisual_patterns)


def character_presence(scene: Scene, characters: list[Character]) -> list[str]:
    """Classify mentions as visual, remote-dialogue, absent, or possessive-only."""
    source = _strip_scene_context(scene.source_text)
    remote_ranges = _remote_dialogue_ranges(source)
    visible: list[str] = []
    for character in characters:
        occurrences = _name_occurrences(source, character.name)
        if not occurrences:
            continue
        visual_evidence = False
        for start, end in occurrences:
            if any(range_start <= start < range_end for range_start, range_end in remote_ranges):
                continue
            window = source[max(0, start - 45) : min(len(source), end + 55)]
            if _mention_is_nonvisual(window, character.name) or _is_communication_only_occurrence(
                window, character.name
            ):
                continue
            visual_evidence = True
            break
        if visual_evidence:
            visible.append(character.id)
    return visible


def _raw_tokens(value: str) -> list[str]:
    return re.findall(r"[\wÀ-ỹ]+", str(value).casefold(), re.UNICODE)


def _prop_identity_alias(prop_name: str) -> list[str]:
    tokens = _raw_tokens(prop_name)
    while tokens and tokens[0] in {"the", "a", "an", "chiếc", "chiec", "cái", "cai"}:
        tokens.pop(0)
    boundary = {"màu", "mau", "color", "colour", "của", "cua", "of", "with", "có", "co"}
    core: list[str] = []
    for token in tokens:
        if token in boundary:
            break
        core.append(token)
    if not core:
        core = tokens[:3]
    if not core:
        return []
    generic_first = {"máy", "may", "device", "object", "item", "đồ", "do"}
    if len(core) == 1:
        return [core[0]]
    if core[0] in generic_first:
        return [" ".join(core[: min(3, len(core))])]
    # Prefer a stable noun phrase, while retaining an exact first-token alias for
    # shortened screenplay mentions. Single-token aliases are Unicode-exact later.
    return [" ".join(core[:2]), core[0]]


def _prop_source_match(source_text: str, prop_name: str) -> bool:
    raw_source = str(source_text).casefold()
    folded_source = semantic_key(source_text)
    for alias in _prop_identity_alias(prop_name):
        raw_alias_tokens = _raw_tokens(alias)
        if len(raw_alias_tokens) == 1:
            token = raw_alias_tokens[0]
            if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", raw_source, re.UNICODE):
                return True
            continue
        alias_key = semantic_key(alias)
        if alias_key and re.search(
            rf"(?<![a-z0-9]){re.escape(alias_key)}(?![a-z0-9])", folded_source
        ):
            return True
    return False


def _prop_negated(source_text: str, prop_name: str) -> bool:
    raw = _strip_scene_context(source_text).casefold()
    for alias in _prop_identity_alias(prop_name):
        alias_raw = alias.casefold().strip()
        if not alias_raw:
            continue
        token = re.escape(alias_raw)
        patterns = (
            rf"(?:không|khong)\s+(?:cầm|cam|mang|có|co|giữ|giu)\s+(?:chiếc\s+|chiec\s+|cái\s+|cai\s+|the\s+)?{token}\b",
            rf"(?:without|not\s+holding|not\s+carrying|does\s+not\s+have)\s+(?:the\s+)?{token}\b",
            rf"\b{token}\b(?:\s+[\wÀ-ỹ]+){{0,3}}(?:\s+vẫn|\s+van)?\s+(?:không|khong)\s+(?:có|co|ở|o|xuất\s+hiện|xuat\s+hien)\b",
            rf"\b{token}\b(?:\s+[A-Za-z]+){{0,3}}\s+(?:is\s+not|isn't|not\s+present|not\s+here)\b",
        )
        if any(re.search(pattern, raw, re.UNICODE) for pattern in patterns):
            return True
    return False


def _physical_source_text(scene: Scene) -> str:
    text = _strip_scene_context(scene.source_text)
    # Dialogue content may mention a prop that is elsewhere. Remove the exact spoken
    # payload before deciding physical presence; source action text remains authoritative.
    for dialogue in scene.dialogues:
        spoken = str(dialogue.text or "").strip()
        if spoken:
            text = re.sub(re.escape(spoken), " ", text, flags=re.IGNORECASE)
    return text


def mentioned_props(scene: Scene, props: list[Prop]) -> set[str]:
    physical = _physical_source_text(scene)
    return {
        item.id
        for item in props
        if _prop_source_match(physical, item.name) and not _prop_negated(physical, item.name)
    }


def _prop_transformed_state(scene: Scene, prop: Prop) -> str | None:
    """Return a source-grounded physical transformation without deleting the prop."""
    source = semantic_key(_physical_source_text(scene))
    aliases = [
        semantic_key(alias) for alias in _prop_identity_alias(prop.name) if semantic_key(alias)
    ]
    for alias in aliases:
        tear_patterns = (
            rf"\bxe\b.{{0,18}}\b{re.escape(alias)}\b.{{0,24}}"
            rf"\b(?:lam\s+doi|thanh\s+hai)\b",
            rf"\b{re.escape(alias)}\b.{{0,18}}\bxe\b.{{0,24}}"
            rf"\b(?:lam\s+doi|thanh\s+hai)\b",
            rf"\b(?:tear|tears|rip|rips)\b.{{0,24}}\b(?:the\s+)?"
            rf"{re.escape(alias)}\b.{{0,16}}\b(?:in\s+half|in\s+two|apart)\b",
            rf"\b{re.escape(alias)}\b.{{0,18}}"
            rf"\b(?:torn\s+in\s+half|ripped\s+in\s+two)\b",
        )
        if any(re.search(pattern, source) for pattern in tear_patterns):
            return (
                f"Source transformation: {prop.name} is torn into two physical pieces; "
                "preserve the resulting fragments until the screenplay explicitly moves, "
                "discards, or destroys them"
            )
    return None


def _prop_removed_by_action(scene: Scene, prop: Prop) -> bool:
    """Remove a prop only when the source explicitly disposes of or destroys it."""
    source = semantic_key(_physical_source_text(scene))
    aliases = [
        semantic_key(alias) for alias in _prop_identity_alias(prop.name) if semantic_key(alias)
    ]
    for alias in aliases:
        patterns = (
            rf"\b(?:vut|nem)\b.{{0,18}}\b{re.escape(alias)}\b.{{0,12}}\b(?:di|bo)\b",
            rf"\b(?:bo\s+lai|pha\s+huy|huy|dot)\b.{{0,18}}\b{re.escape(alias)}\b",
            rf"\b(?:throw\s+away|discard|destroy|burn)\b.{{0,24}}"
            rf"\b(?:the\s+)?{re.escape(alias)}\b",
            rf"\b{re.escape(alias)}\b.{{0,18}}"
            rf"\b(?:destroyed|discarded|burned|thrown\s+away)\b",
        )
        if any(re.search(pattern, source) for pattern in patterns):
            return True
    return False


def _prop_source_state(scene: Scene, prop: Prop) -> str:
    """Describe the physical state using the authored beat before generic fallback text."""
    source = semantic_key(_physical_source_text(scene))
    name_key = semantic_key(prop.name)
    if (
        ("goc phai" in source or "right corner" in source)
        and ("ve" in source or "ticket" in source)
        and ("ve" in name_key or "ticket" in name_key)
    ):
        return (
            f"Source state: right-corner fragment of {prop.name} is physically present; "
            "do not restore the complete ticket"
        )
    transformed = _prop_transformed_state(scene, prop)
    if transformed:
        return transformed
    return f"Present in source beat: {prop.name}; canonical source state: {prop.state}"


def safe_prop_states(
    scene: Scene,
    props: list[Prop],
    previous_props: dict[str, str],
    *,
    direct_continuation: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build physical prop start/end state from source evidence and lifecycle."""
    present = mentioned_props(scene, props)
    by_id = {item.id: item for item in props}
    start_state: dict[str, str] = {}
    if direct_continuation:
        start_state.update({key: value for key, value in previous_props.items() if key in by_id})
    for prop_id in present:
        start_state[prop_id] = _prop_source_state(scene, by_id[prop_id])

    end_state = dict(start_state)
    for prop_id, prop in by_id.items():
        if prop_id not in end_state:
            continue
        transformed = _prop_transformed_state(scene, prop)
        if transformed:
            end_state[prop_id] = transformed
            continue
        if _prop_removed_by_action(scene, prop):
            end_state.pop(prop_id, None)
    return start_state, end_state


def camera_for_scene(scene: Scene, visible_count: int) -> str:
    """Preserve valid AI camera or choose a deterministic compatible variation."""
    camera = scene.camera.strip()
    folded = semantic_key(camera)
    generic = (
        "single subject cinematic shot" in folded
        or "balanced multi subject cinematic composition" in folded
        or "environmental cinematic shot" in folded
    )
    multi_markers = (
        "two shot",
        "two person",
        "two people",
        "both",
        "hai nguoi",
        "group",
        "ensemble",
    )
    conflict = visible_count < 2 and any(marker in folded for marker in multi_markers)
    if camera and not generic and not conflict:
        return camera
    if visible_count == 0:
        palette = _CAMERA_EMPTY
    elif visible_count == 1:
        palette = _CAMERA_SINGLE
    else:
        palette = _CAMERA_MULTI
    return palette[(scene.order - 1) % len(palette)]


def _context_signature(scene: Scene) -> tuple[str, str]:
    match = re.search(r"\[SCENE CONTEXT\](.*?)\[END CONTEXT\]", scene.source_text, re.DOTALL)
    context = semantic_key(match.group(1) if match else "")
    temporal = ""
    for marker in (
        "flashback",
        "hien tai",
        "present",
        "song song",
        "parallel",
        "continuous",
        "lien tuc",
    ):
        if marker in context:
            temporal = marker
            break
    return context, temporal


def _is_direct_continuation(previous_scene: Scene | None, scene: Scene) -> bool:
    if previous_scene is None or previous_scene.location_id != scene.location_id:
        return False
    prev_context, prev_temporal = _context_signature(previous_scene)
    cur_context, cur_temporal = _context_signature(scene)
    prev_flashback = "flashback" in prev_context
    cur_flashback = "flashback" in cur_context
    if prev_flashback != cur_flashback:
        return False
    explicit_present = ("hien tai", "present")
    if prev_flashback and any(marker in cur_context for marker in explicit_present):
        return False
    if cur_flashback and any(marker in prev_context for marker in explicit_present):
        return False
    if "song song" in cur_context or "parallel" in cur_context:
        return False
    return True


def _state_claims_nonvisual(value: str) -> bool:
    folded = semantic_key(value)
    markers = (
        "khong co mat",
        "khong o trong khung hinh",
        "not in frame",
        "not present",
        "not in scene",
        "off screen",
        "offscreen",
        "voice only",
        "giong noi",
    )
    return any(marker in folded for marker in markers)


def normalize_semantic_scene(
    scene: Scene,
    *,
    characters: list[Character],
    props: list[Prop],
    previous_scene: Scene | None,
) -> None:
    scene.characters = character_presence(scene, characters)
    direct = _is_direct_continuation(previous_scene, scene)
    previous_props = previous_scene.end_state.prop_positions if previous_scene and direct else {}
    start_props, end_props = safe_prop_states(
        scene, props, previous_props, direct_continuation=direct
    )
    visible_ids = set(scene.characters)
    character_by_id = {item.id: item for item in characters}
    for state_index, state in enumerate((scene.start_state, scene.end_state)):
        state.character_positions = {
            key: value for key, value in state.character_positions.items() if key in visible_ids
        }
        state.character_wardrobe = {
            key: value for key, value in state.character_wardrobe.items() if key in visible_ids
        }
        for character_id in visible_ids:
            position = state.character_positions.get(character_id, "").strip()
            if not position or _state_claims_nonvisual(position):
                phase = "start" if state_index == 0 else "end"
                state.character_positions[character_id] = (
                    f"Visible in source-grounded {phase} frame at {scene.location_id}; "
                    "blocking follows the authored action"
                )
            wardrobe = state.character_wardrobe.get(character_id, "").strip()
            if not wardrobe or _state_claims_nonvisual(wardrobe):
                character = character_by_id.get(character_id)
                if character is not None:
                    state.character_wardrobe[character_id] = character.clothing
    scene.start_state.prop_positions = dict(start_props)
    scene.end_state.prop_positions = dict(end_props)
    scene.camera = camera_for_scene(scene, len(scene.characters))

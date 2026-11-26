"""Video Studio Flow — planner kịch bản (port từ tool CINEMAFLOW của labs.google Flow).

Tool gốc là applet chạy trong môi trường Flow với SDK riêng (``Flow.generate.*``).
Phần kịch bản được port về đây giữ nguyên logic gốc:

- ``generate_storyboard_with_ai`` — Copy nguyên ``storyboardService.ts``: thử gọi
  LLM (endpoint OpenAI-compatible qua env ``STUDIO_LLM_*``, chủ tool sẽ điền
  .env sau) để viết kịch bản tuỳ chủ đề; LLM fail / chưa cấu hình thì rơi về
- ``generate_deterministic_storyboard`` — Copy nguyên ``storyboardEngine.ts``:
  4 template theo từ khoá (kitchen/workshop/journey/cozy) + universal fallback,
  ``adjust_scene_count`` co giãn về đúng số cảnh yêu cầu (4–25).

Schema kịch bản giữ đúng tool gốc (types.ts) để Import/Export JSON tương thích
hai chiều với applet trên Flow.
"""

import json
import logging
import re
from typing import Optional

import httpx

from .config import (
    STUDIO_LLM_API_KEY,
    STUDIO_LLM_BASE_URL,
    STUDIO_LLM_MODEL,
    STUDIO_LLM_TIMEOUT,
)

logger = logging.getLogger("playground.studio")

# Duration Omni 1.2 Flash hỗ trợ (types.ts supportedDurations); Veo 3.1 Lite
# cố định 8s nhưng planner vẫn ghi durationSeconds theo plan — composer của
# playground sẽ clamp về length model hỗ trợ lúc validate.
SUPPORTED_DURATIONS = (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
MIN_SCENES = 4
MAX_SCENES = 25

# Schema chuẩn của một ScenePlan — dùng để sanitize output LLM và validate
# Import JSON (các field throttle tự do phía tool gốc: reusesStartFromSceneEnd,
# isMasterStart chỉ là hint hiển thị nên không bắt buộc).
SCENE_FIELDS = (
    "id",
    "nameVi",
    "role",
    "durationSeconds",
    "editorialTrimNotes",
    "startPrompt",
    "endPrompt",
    "videoPrompt",
    "scaleAnchorDesc",
    "stateChange",
)

_DIRECTOR_PROMPT = """You are a world-class creative video director and storyboard architect for "Video Studio Flow".
You plan and design realistic cinematic video sequences for ANY topic, genre, or creative vision (cinematic narratives, commercial ads, culinary mastery, craftsmanship, nature & travel, sci-fi, documentaries, etc.). Create high-quality, realistic visual concepts suitable for live-action or cinematic realism (no tiny/miniature constraints, no cartoon/toy restrictions).

Strict storyboard structural rules:
1. Exact scene count: {scene_count} scenes (S01 to S{last_id}).
2. Hook: Scene 01 must be an attention-grabbing hook (durationSeconds: 3 to 5).
3. Body scenes: Progressive, coherent storytelling with clear visual transitions from scene to scene.
4. Reward: Final scene must deliver a satisfying visual payoff, culmination, or climactic result.
5. Strict continuity: S01 starts the story; each subsequent scene S(n) naturally develops and builds upon the preceding scene S(n-1).
6. Camera framing: Professional cinematic framing, clean composition with balanced lighting, zero dark vignetting.
7. Prompts: Complete, descriptive, vivid English generation prompts optimized for 9:16 portrait.
8. Model durations: Supported integer seconds (3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 15).

Topic to produce: "{topic}".

Return ONLY valid JSON matching this structure:
{{
  "category": "creative",
  "scenes": [
    {{
      "id": "S01",
      "nameVi": "Tên cảnh tiếng Việt ngắn gọn",
      "role": "hook",
      "durationSeconds": 3,
      "editorialTrimNotes": "Ghi chú nhịp điệu và biên tập cảnh",
      "startPrompt": "Comprehensive descriptive English prompt for start frame...",
      "endPrompt": "Comprehensive descriptive English prompt for end frame showing clear progression...",
      "videoPrompt": "Dynamic motion prompt guiding the video generation model...",
      "scaleAnchorDesc": "Key visual anchor or focal subject establishing spatial continuity",
      "stateChange": "Mô tả cụ thể sự biến chuyển hành động/trạng thái trong cảnh"
    }}
  ],
  "dependencyExplanation": "Giải thích cấu trúc diễn tiến tuần tự của toàn bộ kịch bản..."
}}"""

_SYSTEM_INSTRUCTION = (
    "You are an expert creative video director and storyboard designer. "
    "Output strictly valid JSON without markdown fences."
)

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


# ──────────────────────────────────────────────────────────────────────────
# Sanitize / normalize (LLM output + Import JSON đi qua cùng một cửa)
# ──────────────────────────────────────────────────────────────────────────


def _clean_text(value, default: str, limit: int = 4000) -> str:
    text = str(value or "").strip()
    return (text or default)[:limit]


def _clean_duration(value, fallback: int = 5) -> int:
    try:
        duration = int(str(value).strip().rstrip("s"))
    except (TypeError, ValueError):
        return fallback
    if duration in SUPPORTED_DURATIONS:
        return duration
    return min(max(duration, 3), 15)


def _clean_role(value, index: int, total: int) -> str:
    role = str(value or "").strip().lower()
    if role in ("hook", "setup", "body", "reward"):
        return role
    if index == 0:
        return "hook"
    if index == total - 1:
        return "reward"
    return "body"


def sanitize_storyboard(raw, topic: str, scene_count: int) -> Optional[dict]:
    """Ép payload bất kỳ (LLM JSON / Import JSON) về schema storyboard chuẩn.

    Trả None nếu không phải dict hoặc scenes không phải list đủ dài — caller
    quyết định fallback. Không ném exception: input là dữ liệu ngoài.
    """
    if not isinstance(raw, dict):
        return None
    raw_scenes = raw.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) < MIN_SCENES:
        return None
    scenes = []
    for index, scene in enumerate(raw_scenes[:MAX_SCENES]):
        if not isinstance(scene, dict):
            continue
        number = index + 1
        scenes.append(
            {
                "id": f"S{number:02d}",
                "nameVi": _clean_text(
                    scene.get("nameVi"), f"Cảnh {number:02d}", 200
                ),
                "role": _clean_role(scene.get("role"), index, len(raw_scenes)),
                "durationSeconds": _clean_duration(
                    scene.get("durationSeconds"), 3 if index == 0 else 5
                ),
                "editorialTrimNotes": _clean_text(
                    scene.get("editorialTrimNotes"), "Biên tập chuyển động mượt mà."
                ),
                "startPrompt": _clean_text(scene.get("startPrompt"), "", 8000),
                "endPrompt": _clean_text(scene.get("endPrompt"), "", 8000),
                "videoPrompt": _clean_text(scene.get("videoPrompt"), "", 8000),
                "scaleAnchorDesc": _clean_text(
                    scene.get("scaleAnchorDesc"),
                    "Vật thể đo tỉ lệ cùng mặt phẳng nét.",
                ),
                "stateChange": _clean_text(
                    scene.get("stateChange"), "Thay đổi trạng thái vật thể."
                ),
            }
        )
    if len(scenes) < MIN_SCENES:
        return None
    if scene_count and len(scenes) > scene_count:
        # LLM/JSON trả thừa cảnh: cắt về đúng yêu cầu, giữ Hook + Reward.
        keep = {0, len(scenes) - 1}
        body_needed = scene_count - 2
        keep.update(range(1, 1 + body_needed))
        scenes = [scenes[i] for i in sorted(keep)[:scene_count]]
        for index, scene in enumerate(scenes):
            scene["id"] = f"S{index + 1:02d}"
    total_duration = sum(scene["durationSeconds"] for scene in scenes)
    return {
        "topic": _clean_text(topic, "", 500),
        "category": _clean_text(raw.get("category"), "creative", 40),
        "totalDurationSeconds": total_duration,
        "scenes": scenes,
        "dependencyExplanation": _clean_text(
            raw.get("dependencyExplanation"),
            "Kịch bản phân cảnh điện ảnh tuần tự Hook → Setup → Body → Reward.",
        ),
    }


# ──────────────────────────────────────────────────────────────────────────
# Planner LLM (storyboardService.ts) — env STUDIO_LLM_* chưa điền thì skip
# ──────────────────────────────────────────────────────────────────────────


def llm_configured() -> bool:
    return bool(STUDIO_LLM_MODEL and STUDIO_LLM_API_KEY)


async def _call_llm_storyboard(topic: str, scene_count: int) -> Optional[dict]:
    """Gọi /v1/chat/completions của endpoint LLM; trả storyboard sanitized.

    Mọi lỗi (mạng, HTTP, JSON hỏng) trả None — caller rơi về deterministic,
    đúng hành vi "falling back to deterministic engine" của tool gốc.
    """
    prompt = _DIRECTOR_PROMPT.format(
        scene_count=scene_count,
        last_id=f"S{scene_count:02d}",
        topic=topic,
    )
    body = {
        "model": STUDIO_LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=STUDIO_LLM_TIMEOUT) as client:
            response = await client.post(
                f"{STUDIO_LLM_BASE_URL}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {STUDIO_LLM_API_KEY}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Studio planner LLM gọi thất bại: %s", exc)
        return None
    if response.status_code >= 400:
        logger.warning(
            "Studio planner LLM trả HTTP %s: %s",
            response.status_code,
            response.text[:200],
        )
        return None
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            return None
        cleaned = _CODE_FENCE_RE.sub("", content.strip())
        parsed = json.loads(cleaned)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Studio planner LLM trả JSON không đọc được: %s", exc)
        return None
    return sanitize_storyboard(parsed, topic, scene_count)


# ──────────────────────────────────────────────────────────────────────────
# Deterministic engine (storyboardEngine.ts) — port 1:1
# ──────────────────────────────────────────────────────────────────────────


def _adjust_scene_count(base_scenes, target_count: int, topic: str):
    if len(base_scenes) == target_count:
        return [dict(scene) for scene in base_scenes]

    count = max(MIN_SCENES, target_count)

    if count < len(base_scenes):
        result = [base_scenes[0], base_scenes[1]]  # Hook + Setup
        body_needed = count - 3
        for i in range(body_needed):
            result.append(base_scenes[2 + i])
        result.append(base_scenes[-1])  # Final reward
        return _renumber(result)

    # Expand body scenes — chèn xen 3 vị trí sau Setup như tool gốc.
    result = list(base_scenes)
    diff = count - len(base_scenes)
    for i in range(diff):
        insert_idx = 3 + (i % 3)
        result.insert(
            insert_idx,
            {
                "id": f"S_EXTRA_{i}",
                "nameVi": f"Cảnh bổ trợ {i + 1}: Chuyển động chi tiết ({topic})",
                "role": "body",
                "durationSeconds": 5,
                "editorialTrimNotes": (
                    "Chuyển động quay cận cảnh tiếp nối nhịp nhàng trong quy trình quay phim."
                ),
                "startPrompt": (
                    f"Cinematic 9:16 vertical realistic scene, professional 35mm film photography. "
                    f"A detailed focused shot advancing the visual narrative of {topic}. "
                    f"Elegant natural lighting, rich atmospheric depth, locked smooth composition."
                ),
                "endPrompt": (
                    f"Cinematic 9:16 vertical realistic scene. The progression of {topic} reaches "
                    f"a clear new stage, refined textures and balanced studio illumination."
                ),
                "videoPrompt": (
                    f"Smooth realistic camera motion: fluid cinematic transition capturing the "
                    f"physical progression of {topic}, natural sound design, dynamic realism."
                ),
                "stateChange": f"Hoàn tất giai đoạn bổ trợ {i + 1} của tiến trình.",
                "scaleAnchorDesc": "Tiêu điểm không gian và ánh sáng chủ đạo",
            },
        )
    return _renumber(result)


# nameVi gốc mang số cũ ("Cảnh 01", "Cảnh bổ trợ 1"...) — renumber theo vị trí
# mới sau khi co giãn, đúng regex replace của adjustSceneCount gốc.
_SCENE_NAME_NUMBER_RE = re.compile(r"S\d+|Cảnh \d+|Cảnh phụ \d+|Cảnh bổ trợ \d+")


def _renumber(scenes):
    return [
        {
            **scene,
            "id": f"S{i + 1:02d}",
            "nameVi": _SCENE_NAME_NUMBER_RE.sub(f"Cảnh {i + 1:02d}", scene["nameVi"]),
        }
        for i, scene in enumerate(scenes)
    ]


def _wrap_storyboard(category: str, topic: str, scenes, explanation: str, count: int) -> dict:
    final_scenes = _adjust_scene_count(scenes, count, topic)
    total_duration = sum(scene["durationSeconds"] for scene in final_scenes)
    return {
        "topic": topic,
        "category": category,
        "totalDurationSeconds": total_duration,
        "scenes": final_scenes,
        "dependencyExplanation": explanation,
    }


def _kitchen_scenes(topic: str, count: int):
    subject = topic.strip() or "artisan culinary dish"
    long_run = count >= 8
    short = 4 if not long_run else None
    scenes = [
        {
            "id": "S01",
            "nameVi": "Cảnh 01: Móc câu điện ảnh (Hook khai màn)",
            "role": "hook",
            "durationSeconds": 3,
            "editorialTrimNotes": (
                "Nhát cắt nhanh 3s mở đầu, món ăn hoàn hảo bắt mắt thu hút thị giác trước khi mở quy trình."
            ),
            "startPrompt": (
                f"Cinematic 9:16 vertical high-end food commercial shot. A masterfully prepared {subject} "
                f"displayed on a modern ceramic plate, steam gently rising, golden highlights and rich "
                f"glossy sauce under warm cinematic studio lighting. Crisp depth of field, locked elegant framing."
            ),
            "endPrompt": (
                f"Cinematic 9:16 vertical commercial shot. A polished silver serving fork or utensil smoothly "
                f"portions a bite of {subject}, revealing moist tender crumb and delicious layers, warm "
                f"appetizing ambience."
            ),
            "videoPrompt": (
                f"Smooth cinematic slow-motion movement, 9:16 portrait. A silver utensil gently glides into "
                f"the steaming hot {subject}, glistening sauce pools gracefully, natural culinary Foley sound."
            ),
            "stateChange": "Món ăn bắt mắt được chạm nhẹ tách lát hấp dẫn.",
            "scaleAnchorDesc": "Đĩa sứ trắng và ánh sáng ấm áp chủ đạo",
        },
        {
            "id": "S02",
            "nameVi": "Cảnh 02: Khởi tạo bàn bếp (Setup)",
            "role": "setup",
            "durationSeconds": 5 if long_run else short,
            "editorialTrimNotes": (
                "Chuyển cảnh dứt khoát về gian bếp chuyên nghiệp, các nguyên liệu tươi mới sẵn sàng."
            ),
            "startPrompt": (
                "Cinematic 9:16 vertical master shot of an immaculate gourmet kitchen marble counter. "
                "A premium heavy-bottomed pan gently warming on an induction stove, pristine culinary "
                "prep bowls aligned. Warm ambient morning light."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical view of the warm kitchen station. A golden pat of pure butter "
                "smoothly melts across the pan surface, creating a shimmering aromatic glaze."
            ),
            "videoPrompt": (
                "Artisan culinary sequence: butter slides across the hot surface and gently froths into "
                "a golden veil, pristine studio audio of subtle pan sizzle."
            ),
            "stateChange": "Chảo nóng lên và lớp bơ tan chảy lấp lánh sẵn sàng nấu.",
            "scaleAnchorDesc": "Chảo chuyên dụng và mặt bếp đá cẩm thạch",
        },
        {
            "id": "S03",
            "nameVi": "Cảnh 03: Rót nguyên liệu & Tạo hình",
            "role": "body",
            "durationSeconds": 6 if long_run else short,
            "editorialTrimNotes": "Rót đều nguyên liệu chính vào chảo nóng, bắt đầu phản ứng chín.",
            "startPrompt": (
                "Cinematic 9:16 vertical kitchen shot. Warm pan glistening with melted butter. "
                "A stainless steel ladle positioned gracefully above the cooking surface."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical culinary shot. Fresh batter pours smoothly into the center of "
                "the pan, forming a round, even disk as edges immediately begin to bubble and cook."
            ),
            "videoPrompt": (
                "Slow-motion pour: creamy batter streams downward in a steady golden ribbon, spreading "
                "into an even circle with delicate bubbling around the perimeter."
            ),
            "stateChange": "Bột được rót đều tạo hình đĩa bánh tròn hoàn hảo.",
            "scaleAnchorDesc": "Viền chảo và đĩa tròn trung tâm",
        },
        {
            "id": "S04",
            "nameVi": "Cảnh 04: Gia nhiệt & Nở xốp",
            "role": "body",
            "durationSeconds": 6 if long_run else short,
            "editorialTrimNotes": "Mặt bánh sủi bọt khí xốp nhẹ, viền vàng ruộm caramel thơm lừng.",
            "startPrompt": (
                "Cinematic 9:16 vertical close-up. The cooking disk rests in sizzling butter, delicate "
                "bubbles rising toward the surface, edges firming up."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical culinary close-up. The surface has aerated into a fluffy, set "
                "texture while the base develops a deep golden-brown toasted crust."
            ),
            "videoPrompt": (
                "Close-up cooking transition: aerated steam bubbles burst gently across the surface, "
                "the rim solidifies into a rich caramelized crust."
            ),
            "stateChange": "Bột chín nở phồng xốp, viền bánh giòn thơm.",
            "scaleAnchorDesc": "Mặt phẳng bánh nở đều trong lòng chảo",
        },
        {
            "id": "S05",
            "nameVi": "Cảnh 05: Lật mặt bánh chuẩn xác",
            "role": "body",
            "durationSeconds": 5 if long_run else short,
            "editorialTrimNotes": "Thao tác lật bánh chuyên nghiệp, để lộ mặt nướng vàng óng.",
            "startPrompt": (
                "Cinematic 9:16 vertical shot. A chef spatula slips cleanly under the cooked base, "
                "ready for the flip."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. The flip is complete, revealing an even, golden-tan "
                "caramelized surface with delicate honeycomb pattern."
            ),
            "videoPrompt": (
                "Crisp chef motion: the spatula lifts and turns the pancake cleanly in mid-air, landing "
                "with a soft sizzling tap, faint wisp of aroma steam rising."
            ),
            "stateChange": "Lật bánh 180 độ, lộ mặt chín vàng nâu hoàn mỹ.",
            "scaleAnchorDesc": "Tâm chảo và xẻng inox sáng bóng",
        },
        {
            "id": "S06",
            "nameVi": "Cảnh 06: Bày ra đĩa sứ cao cấp",
            "role": "body",
            "durationSeconds": 5 if long_run else short,
            "editorialTrimNotes": "Chuyển thành phẩm từ chảo sang đĩa sứ nghệ thuật.",
            "startPrompt": (
                "Cinematic 9:16 vertical kitchen view. The golden fully-cooked culinary piece rests hot "
                "in the pan, adjacent to an elegant serving plate."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical view. The dish is now centered majestically on the modern "
                "ceramic plate, steaming gently in warm dining room light."
            ),
            "videoPrompt": (
                "Plating motion: the spatula smoothly transfers the fluffy creation onto the ceramic "
                "plate with gentle precision, soft porcelain contact sound."
            ),
            "stateChange": "Bánh được bày trí trang nhã lên đĩa phục vụ.",
            "scaleAnchorDesc": "Đĩa sứ trắng viền vàng thanh lịch",
        },
        {
            "id": "S07",
            "nameVi": "Cảnh 07: Rưới sốt & Hoàn thiện hương vị",
            "role": "reward",
            "durationSeconds": 5 if long_run else short,
            "editorialTrimNotes": (
                "Dòng siro hoặc mật ong óng ả rưới đều làm nổi bật độ ẩm mọng."
            ),
            "startPrompt": (
                "Cinematic 9:16 vertical presentation shot. The warm fluffy stack sits cleanly on the "
                "plate, soft diffused illumination."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical presentation shot. Rich, translucent amber syrup pools gracefully "
                "across the crown and cascades down the sides in glossy ribbons."
            ),
            "videoPrompt": (
                "Slow-motion drizzle: thick golden syrup cascades down from above in an elegant spiral, "
                "catching warm reflections as it enriches the golden surface."
            ),
            "stateChange": "Lớp sốt óng ánh phủ đều lên món ăn.",
            "scaleAnchorDesc": "Dòng chảy óng ánh và đĩa sứ trung tâm",
        },
        {
            "id": "S08",
            "nameVi": "Cảnh 08: Thưởng thức & Cao trào (Reward)",
            "role": "reward",
            "durationSeconds": 5 if long_run else short,
            "editorialTrimNotes": (
                "Thành quả hoàn mỹ cuối cùng, đem lại cảm giác ngon miệng và thỏa mãn tột đỉnh."
            ),
            "startPrompt": (
                "Cinematic 9:16 vertical commercial hero shot. The fully dressed culinary masterpiece "
                "gleams with appetite-inducing perfection under studio lights."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical hero shot. A clean cut reveals the airy, cloud-like interior "
                "texture, golden sauce glistening along the slice. Ultimate culinary gratification."
            ),
            "videoPrompt": (
                "Hero finale: smooth camera tilt and gentle glide revealing the fluffy texture, "
                "appetizing steam motes, rich warm sound atmosphere."
            ),
            "stateChange": "Món ăn hoàn thiện 100%, đem lại sự thỏa mãn thị giác tuyệt đối.",
            "scaleAnchorDesc": "Toàn cảnh đĩa ăn đạt chuẩn nhà hàng cao cấp",
        },
    ]
    return scenes, (
        "Quy trình sản xuất video ẩm thực điện ảnh: S01 Hook độc lập → S02 Setup bối cảnh → "
        "S03-S06 Các công đoạn chế biến tuần tự → S07-S08 Hoàn thiện & Thưởng thức."
    )


def _workshop_scenes(topic: str, count: int):
    scenes = [
        {
            "id": "S01",
            "nameVi": "Cảnh 01: Móc câu (Kiểm tra khớp nối tinh xảo)",
            "role": "hook",
            "durationSeconds": 3,
            "editorialTrimNotes": (
                "Khung hình 3s đầu làm nổi bật độ hoàn thiện chính xác của sản phẩm thủ công."
            ),
            "startPrompt": (
                "Cinematic 9:16 vertical shot of a master woodworker bench. A finely sculpted walnut "
                "wood joint rests under warm studio raking light highlighting rich natural grain. "
                "Shallow depth of field, sharp detail."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. A brass dowel pin is smoothly tapped flush with the wood "
                "surface, forming a seamless structural joint, dust motes in sunbeams."
            ),
            "videoPrompt": (
                "Mastercraft slow-motion: wooden mallet taps the brass pin flush into the joint with a "
                "solid, resonant wooden click, crisp tactile realism."
            ),
            "stateChange": "Chốt mộng gỗ được gõ khít hoàn hảo vào khớp nối.",
            "scaleAnchorDesc": "Mặt bàn thợ mộc và thước kẹp cơ khí",
        },
        {
            "id": "S02",
            "nameVi": "Cảnh 02: Chuẩn bị phôi liệu gỗ (Setup)",
            "role": "setup",
            "durationSeconds": 5,
            "editorialTrimNotes": "Bắt đầu từ khối gỗ thô tự nhiên kẹp trên ê-tô xưởng mộc.",
            "startPrompt": (
                "Cinematic 9:16 vertical workshop shot. A solid block of raw dark walnut clamped firmly "
                "in a heavy bench vise, artisan hand tools organized neatly."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical workshop shot. Clean pencil measurement lines and mortise "
                "guides are now precisely marked across the timber face."
            ),
            "videoPrompt": (
                "Artisan marking: steel gauge glides along the timber edge, scoring sharp accurate "
                "layout lines into the wood surface with whisper-quiet precision."
            ),
            "stateChange": "Khối gỗ được lấy dấu đo đạc chính xác.",
            "scaleAnchorDesc": "Mặt phôi gỗ và thước đo kim loại",
        },
        {
            "id": "S03",
            "nameVi": "Cảnh 03: Bào mịn bề mặt",
            "role": "body",
            "durationSeconds": 5,
            "editorialTrimNotes": "Lưỡi bào lướt trên gỗ, cuộn những dải phoi mỏng như lụa.",
            "startPrompt": (
                "Cinematic 9:16 vertical workshop shot. Hand plane positioned at the edge of the marked "
                "walnut timber block."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical workshop shot. The plane traverses the piece, a paper-thin "
                "translucent wood shaving curling gracefully upward, revealing satiny smooth grain "
                "underneath."
            ),
            "videoPrompt": (
                "Tactile woodworking motion: hand plane glides forward with a satisfying rhythmic "
                "shaving sound, peeling off an ultra-thin ribbon of walnut grain."
            ),
            "stateChange": "Bề mặt gỗ được bào phẳng mịn như lụa.",
            "scaleAnchorDesc": "Thân bào gỗ và dải phoi cong mềm mại",
        },
        {
            "id": "S04",
            "nameVi": "Cảnh 04: Đục mộng chính xác",
            "role": "body",
            "durationSeconds": 5,
            "editorialTrimNotes": "Mũi đục sắc bén khoét rãnh mộng vuông vức.",
            "startPrompt": (
                "Cinematic 9:16 vertical shot. Hardened steel chisel positioned along the layout line "
                "on the walnut piece."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. A crisp rectangular mortise cavity is excavated into the "
                "wood with razor-sharp walls, clean wood chips resting beside."
            ),
            "videoPrompt": (
                "Woodcraft chisel motion: chisel taps downward with rhythmic firmness, excavating clean "
                "geometric facets into the dark timber."
            ),
            "stateChange": "Rãnh mộng được đục sâu vuông vắn.",
            "scaleAnchorDesc": "Rãnh mộng và mũi đục thép",
        },
        {
            "id": "S05",
            "nameVi": "Cảnh 05: Ghép mộng & Lắp ráp",
            "role": "body",
            "durationSeconds": 5,
            "editorialTrimNotes": "Hai chi tiết gỗ ghép vào nhau vuông góc 90 độ khít khao.",
            "startPrompt": (
                "Cinematic 9:16 vertical view. The tenon rail aligns precisely over the mortise slot."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical view. The joint is fully seated at a strict 90-degree right "
                "angle, forming a rigid interlocking corner."
            ),
            "videoPrompt": (
                "Assembly action: the two components slide firmly together with friction-fit tightness, "
                "seating with a satisfying solid wood thud."
            ),
            "stateChange": "Khung gỗ ghép nối thành công bền chặt.",
            "scaleAnchorDesc": "Khớp nối chữ L vuông vức",
        },
        {
            "id": "S06",
            "nameVi": "Cảnh 06: Đánh sáp hoàn thiện",
            "role": "reward",
            "durationSeconds": 5,
            "editorialTrimNotes": "Thoa sáp tự nhiên làm nổi bật vân gỗ óng ả.",
            "startPrompt": (
                "Cinematic 9:16 vertical view. The assembled wooden frame sits ready on the workbench "
                "in raw unfinished state."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical view. The entire walnut structure is buffed with natural "
                "beeswax, radiating a deep, rich satin amber luster."
            ),
            "videoPrompt": (
                "Buffing finish: soft cotton cloth circles smoothly over the walnut face, instantly "
                "deepening the wood into rich chocolate hues with a soft warm sheen."
            ),
            "stateChange": "Bề mặt gỗ bóng bẩy ánh satin sang trọng.",
            "scaleAnchorDesc": "Màu gỗ óng ả và ánh đèn xưởng mộc",
        },
    ]
    return scenes, (
        "Chuỗi sản xuất xưởng chế tác thủ công: S01 Hook ấn tượng → S02 Chuẩn bị phôi → "
        "S03-S05 Chế tác lắp ghép → S06 Đánh sáp hoàn mỹ."
    )


def _journey_scenes(topic: str, count: int):
    scenes = [
        {
            "id": "S01",
            "nameVi": "Cảnh 01: Móc câu (Vượt địa hình hùng vĩ)",
            "role": "hook",
            "durationSeconds": 3,
            "editorialTrimNotes": (
                "Cú máy mở màn ngoạn mục bắt trọn khoảnh khắc phương tiện chinh phục đỉnh dốc."
            ),
            "startPrompt": (
                "Cinematic 9:16 vertical shot of a rugged expedition vehicle conquering a rocky mountain "
                "crest at sunrise. Dramatic side golden hour lighting, lens flare, dust swirling behind tires."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. The vehicle crests the ridge, headlights cutting through "
                "mountain mist as panoramic peaks unfold in the background."
            ),
            "videoPrompt": (
                "Dynamic cinematic action: tires grip the rugged rock face, powerful momentum carrying "
                "the vehicle up and over the ridge into the golden sunrise."
            ),
            "stateChange": "Phương tiện vượt qua địa hình hiểm trở lên đỉnh núi.",
            "scaleAnchorDesc": "Dãy núi hùng vĩ và cung đường đèo",
        },
        {
            "id": "S02",
            "nameVi": "Cảnh 02: Khởi hành từ bình minh (Setup)",
            "role": "setup",
            "durationSeconds": 5,
            "editorialTrimNotes": "Khởi đầu chuyến đi từ màn sương sớm giữa thung lũng nguyên sơ.",
            "startPrompt": (
                "Cinematic 9:16 vertical landscape shot. A quiet mountain road winding through misty "
                "pine forests, early morning blue hour."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical landscape shot. Warm LED headlights ignite through the morning "
                "mist, vehicle rolling smoothly onto the trail."
            ),
            "videoPrompt": (
                "Dawn departure: headlights pierce the soft morning fog, vehicle accelerates steadily "
                "along the winding asphalt road."
            ),
            "stateChange": "Xe lăn bánh bắt đầu chuyến hành trình.",
            "scaleAnchorDesc": "Cung đường nhựa uốn lượn ven rừng",
        },
        {
            "id": "S03",
            "nameVi": "Cảnh 03: Vượt qua dòng suối đá",
            "role": "body",
            "durationSeconds": 5,
            "editorialTrimNotes": "Lội qua dòng nước trong vắt, nước bắn tung tóe dưới ánh mặt trời.",
            "startPrompt": (
                "Cinematic 9:16 vertical shot. Vehicle approaches a crystal-clear shallow mountain "
                "stream crossing."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. Water cascades around the all-terrain tires, droplets "
                "sparkling like diamonds in the sunbeams."
            ),
            "videoPrompt": (
                "Splash motion: vehicle rolls boldly into the riverbed, splashing dynamic arcs of water "
                "that catch the ambient sunlight."
            ),
            "stateChange": "Vượt qua suối đá thành công.",
            "scaleAnchorDesc": "Dòng nước chảy xiết và bãi cuội",
        },
        {
            "id": "S04",
            "nameVi": "Cảnh 04: Điểm cắm trại hoàng hôn (Reward)",
            "role": "reward",
            "durationSeconds": 5,
            "editorialTrimNotes": "Dừng chân bên vách núi, dựng trại ngắm hoàng hôn rực rỡ.",
            "startPrompt": (
                "Cinematic 9:16 vertical travel hero shot. The vehicle parked securely on a scenic "
                "overlook plateau at twilight, warm campfire crackling."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical hero shot. Glowing campfire embers throw dancing warm light "
                "across the campsite under a canopy of evening stars. Pure serenity."
            ),
            "videoPrompt": (
                "Sunset campfire payoff: gentle camera glide capturing crackling campfire embers, warm "
                "golden light contrasting with twilight starry sky."
            ),
            "stateChange": "Cắm trại thảnh thơi sau hành trình dài.",
            "scaleAnchorDesc": "Ngọn lửa trại bập bùng và chân trời hoàng hôn",
        },
    ]
    return scenes, (
        "Kịch bản hành trình điện ảnh: S01 Hook đỉnh dốc → S02 Khởi hành sương sớm → "
        "S03 Thử thách địa hình → S04 Hoàng hôn viên mãn."
    )


def _cozy_scenes(topic: str, count: int):
    scenes = [
        {
            "id": "S01",
            "nameVi": "Cảnh 01: Móc câu (Ngọn nến sáp thắp sáng)",
            "role": "hook",
            "durationSeconds": 3,
            "editorialTrimNotes": (
                "Ánh sáng ngọn nến bừng lên trong căn phòng ấm áp bên khung cửa sổ mưa."
            ),
            "startPrompt": (
                "Cinematic 9:16 vertical cozy interior shot. An artisan beeswax candle on a dark oak "
                "coffee table, rain droplets running down windowpane behind, soft bokeh of city lights."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical cozy shot. The wick ignites into a radiant golden flame, "
                "casting comforting warm amber light across the room."
            ),
            "videoPrompt": (
                "Warm ignition: the candle wick catches flame smoothly, blooming into a steady golden "
                "teardrop flame with soothing ambient rain patter sound."
            ),
            "stateChange": "Ngọn nến bừng sáng xua tan giá lạnh.",
            "scaleAnchorDesc": "Khung cửa sổ mưa và bàn gỗ sồi",
        },
        {
            "id": "S02",
            "nameVi": "Cảnh 02: Bàn đọc sách yên tĩnh (Setup)",
            "role": "setup",
            "durationSeconds": 5,
            "editorialTrimNotes": "Bối cảnh góc đọc sách thư thái bên ánh nến lung linh.",
            "startPrompt": (
                "Cinematic 9:16 vertical shot. A handcrafted ceramic teacup and a hardcover book resting "
                "on the wooden table, candlelight dancing gently."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. Dried chamomile flower blossoms are placed into the "
                "teacup, ready for fresh hot water."
            ),
            "videoPrompt": (
                "Gentle tea preparation: delicate flower buds settle softly into the cup, serene "
                "peaceful atmosphere with soft rain in background."
            ),
            "stateChange": "Chuẩn bị tách trà hoa cúc thư giãn.",
            "scaleAnchorDesc": "Tách gốm mộc và quyển sách bìa da",
        },
        {
            "id": "S03",
            "nameVi": "Cảnh 03: Rót trà nóng thơm lừng",
            "role": "body",
            "durationSeconds": 5,
            "editorialTrimNotes": "Dòng nước sôi rót vào tách, khói thơm nghi ngút.",
            "startPrompt": (
                "Cinematic 9:16 vertical shot. A copper kettle spout tilts gently over the ceramic cup."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical shot. Steaming amber tea fills the cup, chamomile blossoms "
                "floating serenely, fragrant steam rising."
            ),
            "videoPrompt": (
                "Steaming pour: hot golden tea pours in a smooth graceful arc, swirling the flowers and "
                "releasing faint aromatic steam plumes."
            ),
            "stateChange": "Tách trà ấm nóng thơm ngát sẵn sàng.",
            "scaleAnchorDesc": "Dòng nước trà vàng óng",
        },
        {
            "id": "S04",
            "nameVi": "Cảnh 04: Thư thái bên trang sách (Reward)",
            "role": "reward",
            "durationSeconds": 5,
            "editorialTrimNotes": "Mở trang sách đọc dưới ánh đèn dịu nhẹ, bình yên tuyệt đối.",
            "startPrompt": (
                "Cinematic 9:16 vertical cozy hero shot. Steaming tea next to an open book, warm wool "
                "blanket, rain pattering gently outside."
            ),
            "endPrompt": (
                "Cinematic 9:16 vertical hero shot. A page turns smoothly in slow motion, warm lamplight "
                "illuminating the textured paper. Pure tranquil comfort."
            ),
            "videoPrompt": (
                "Cozy finale: page turns with a soft crisp rustle, camera slowly glides across the "
                "steaming cup and dancing candle flame, soothing ambient calm."
            ),
            "stateChange": "Khoảnh khắc bình yên trọn vẹn bên trang sách.",
            "scaleAnchorDesc": "Quyển sách mở và ngọn nến lung linh",
        },
    ]
    return scenes, (
        "Kịch bản không gian thư thái ấm cúng: S01 Thắp nến → S02 Chuẩn bị góc đọc sách → "
        "S03 Rót trà thơm → S04 Bình yên trọn vẹn."
    )


# Keyword lists giữ nguyên thứ tự ưu tiên như TEMPLATES trong storyboardEngine.ts.
_TEMPLATES = (
    (
        "kitchen",
        (
            "pancake", "bánh", "trứng", "nấu", "cook", "kitchen", "egg", "bếp",
            "coffee", "cà phê", "soup", "pasta", "bánh mì", "nướng", "bake",
            "pie", "pizza", "tea", "trà", "honey", "mật ong", "ẩm thực", "steak",
        ),
        _kitchen_scenes,
    ),
    (
        "workshop",
        (
            "gỗ", "wood", "carpenter", "chế tác", "craft", "workshop", "thợ mộc",
            "búa", "đinh", "ghế", "chair", "table", "bàn", "leather", "da",
            "knife", "dao", "clock", "đồng hồ", "tool", "xưởng", "sản xuất", "chế tạo",
        ),
        _workshop_scenes,
    ),
    (
        "journey",
        (
            "journey", "hành trình", "travel", "du lịch", "xe", "car", "road",
            "đường", "rừng", "forest", "núi", "mountain", "biển", "ocean",
            "phượt", "camping", "cắm trại", "offroad",
        ),
        _journey_scenes,
    ),
    (
        "cozy",
        (
            "cozy", "ấm", "mưa", "rain", "sách", "book", "đèn", "lamp", "trà",
            "nến", "candle", "bedroom", "phòng", "mèo", "cat", "len", "yarn",
            "knitting", "thư viện", "room", "thư giãn",
        ),
        _cozy_scenes,
    ),
)

_UNIVERSAL_STEP_ACTIONS = (
    ("Tạo hình & Phát triển chi tiết", "shaping and refining elements"),
    ("Lắp ráp & Phối hợp các thành phần", "assembling and coordinating components"),
    ("Hoàn thiện bề mặt & Tinh chỉnh ánh sáng", "polishing surfaces and fine-tuning lighting"),
    ("Kích hoạt chuyển động & Tương tác", "activating dynamic elements and interaction"),
    ("Gia cố & Cân chỉnh bố cục", "reinforcing structure and visual balance"),
)


def generate_deterministic_storyboard(topic: str, scene_count: int) -> Optional[dict]:
    """Port của generateDeterministicStoryboard — trả None nếu chủ đề rỗng."""
    clean_topic = topic.strip().lower()
    if not clean_topic:
        return None
    count = min(MAX_SCENES, max(MIN_SCENES, int(scene_count or MIN_SCENES)))
    for category, keywords, builder in _TEMPLATES:
        if any(keyword in clean_topic for keyword in keywords):
            scenes, explanation = builder(topic, count)
            return _wrap_storyboard(category, topic, scenes, explanation, count)

    # Universal fallback — kịch bản thích nghi mọi chủ đề (giữ tool gốc).
    target_count = count
    subject = topic.strip()
    scenes = [
        {
            "id": "S01",
            "nameVi": f"Cảnh 01: Móc câu mở đầu ({subject})",
            "role": "hook",
            "durationSeconds": 3,
            "editorialTrimNotes": (
                "Nhát cắt nhanh 3s đầu thu hút sự chú ý, tạo điểm nhấn cao trào trước khi mở quy trình."
            ),
            "startPrompt": (
                f"Cinematic 9:16 vertical realistic scene, professional commercial photography. "
                f"A captivating establishing setup featuring {subject} with intricate realism, "
                f"elegant directional lighting, crisp depth of field."
            ),
            "endPrompt": (
                f"Cinematic 9:16 vertical realistic scene, locked camera angle. The core element of "
                f"{subject} exhibits an eye-catching transformation, catching the light with clean "
                f"balanced illumination."
            ),
            "videoPrompt": (
                f"Smooth realistic camera motion, 9:16 portrait. Dynamic action unfolds on {subject} "
                f"with fluid cinematic progression, rich diegetic sound."
            ),
            "stateChange": f"Khởi đầu trực quan ấn tượng của {subject}.",
            "scaleAnchorDesc": "Tiêu điểm trung tâm và nguồn sáng chủ đạo",
        },
        {
            "id": "S02",
            "nameVi": "Cảnh 02: Khởi tạo bối cảnh chính",
            "role": "setup",
            "durationSeconds": 5,
            "editorialTrimNotes": (
                "Chuyển cảnh dứt khoát (hard cut) từ Hook về bối cảnh ban đầu, sẵn sàng cho các công đoạn."
            ),
            "startPrompt": (
                f"Cinematic 9:16 vertical master shot of the environment prepared for {subject}. "
                f"Clean modern space, equipment aligned, balanced studio lighting."
            ),
            "endPrompt": (
                f"Cinematic 9:16 vertical master shot. The foundational elements for {subject} are "
                f"now positioned centrally, ready for development. Natural ambient illumination."
            ),
            "videoPrompt": (
                "Cinematic setup sequence: essential elements smoothly assemble into the center "
                "frame with steady camera motion."
            ),
            "stateChange": f"Bối cảnh và nguyên liệu cơ bản cho {subject} đã vào vị trí.",
            "scaleAnchorDesc": "Bố cục không gian trung tâm",
        },
    ]
    body_count = target_count - 3
    for i in range(body_count):
        title, verb = _UNIVERSAL_STEP_ACTIONS[i % len(_UNIVERSAL_STEP_ACTIONS)]
        scene_num = i + 3
        scenes.append(
            {
                "id": f"S{scene_num:02d}",
                "nameVi": f"Cảnh {scene_num:02d}: {title}",
                "role": "body",
                "durationSeconds": 5,
                "editorialTrimNotes": (
                    f"Công đoạn {i + 1} của quy trình thực hiện {subject}."
                ),
                "startPrompt": (
                    f"Cinematic 9:16 vertical realistic scene. {subject} during {verb}, components "
                    f"aligned, natural cinematic lighting, locked smooth composition."
                ),
                "endPrompt": (
                    f"Cinematic 9:16 vertical realistic scene. Step {i + 1} complete on {subject}, "
                    f"visible physical advancement and polished textures."
                ),
                "videoPrompt": (
                    "Realistic cinematic sequence: fluid movements progress the state of "
                    f"{subject} with authentic physical action and natural acoustics."
                ),
                "stateChange": f"Hoàn tất giai đoạn: {title}.",
                "scaleAnchorDesc": "Bố cục trung tâm và chi tiết tiến trình",
            }
        )
    scenes.append(
        {
            "id": f"S{target_count:02d}",
            "nameVi": f"Cảnh {target_count:02d}: Thành quả hoàn mỹ (Reward)",
            "role": "reward",
            "durationSeconds": 5,
            "editorialTrimNotes": (
                "Toàn cảnh thành phẩm hoàn thiện chứng minh kết quả mỹ mãn của quy trình."
            ),
            "startPrompt": (
                f"Cinematic 9:16 vertical hero shot. Completed {subject} presented in pristine "
                f"cinematic lighting, high-end realism and immaculate detail."
            ),
            "endPrompt": (
                f"Cinematic 9:16 vertical hero shot. Final showcase shot of completed {subject}, "
                f"gleaming highlights, pure visual satisfaction."
            ),
            "videoPrompt": (
                f"Cinematic payoff: camera holds steady on the complete masterpiece of {subject}, "
                f"subtle atmospheric lighting, rich rewarding finale."
            ),
            "stateChange": (
                f"Thành phẩm {subject} hoàn thiện 100%, đem lại cảm giác thỏa mãn trọn vẹn."
            ),
            "scaleAnchorDesc": "Toàn cảnh tác phẩm hoàn chỉnh",
        }
    )
    return _wrap_storyboard(
        "workshop",
        topic,
        scenes,
        f'Quy trình sản xuất điện ảnh cho chủ đề "{subject}": Đồ thị quan hệ nhân quả tuần tự '
        f"từ Hook → Setup → Các công đoạn thân → Thành quả hoàn mỹ.",
        target_count,
    )


async def generate_storyboard_with_ai(topic: str, scene_count: int) -> dict:
    """Cửa vào chính cho planner — port generateStoryboardWithAi của tool gốc.

    LLM (env STUDIO_LLM_*) fail hoặc chưa cấu hình → deterministic engine.
    Trả {"storyboard": {...}, "source": "llm" | "template"} hoặc
    {"error": ...} khi chủ đề rỗng.
    """
    clean_topic = (topic or "").strip()
    if not clean_topic:
        return {"error": 'Vui lòng nhập chủ đề vào ô "Hôm nay sản xuất gì".'}
    scene_count = min(max(int(scene_count or 8), MIN_SCENES), MAX_SCENES)

    if llm_configured():
        storyboard = await _call_llm_storyboard(clean_topic, scene_count)
        if storyboard:
            return {"storyboard": storyboard, "source": "llm"}
        logger.info("Planner LLM không dùng được — rơi về engine deterministic.")

    storyboard = generate_deterministic_storyboard(clean_topic, scene_count)
    if storyboard is None:
        return {"error": "Vui lòng nhập chủ đề sản xuất."}
    return {"storyboard": storyboard, "source": "template"}


def import_storyboard(raw, topic_hint: str = "") -> dict:
    """Validate Import JSON từ frontend về schema chuẩn — lỗi trả error."""
    hint = ""
    if isinstance(raw, dict):
        hint = str(raw.get("topic") or "").strip()
    storyboard = sanitize_storyboard(raw, hint or topic_hint.strip(), 0)
    if storyboard is None:
        return {"error": "JSON kịch bản không đúng schema Video Studio Flow (thiếu scenes ≥ 4)."}
    return {"storyboard": storyboard, "source": "import"}

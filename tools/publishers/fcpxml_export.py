"""Export a scene_plan/edit_decisions cut to FCPXML for DaVinci Resolve import.

This is a PUBLISH-tier `publish` tool — a second `provider` under the same
capability as `export_bundle`. Where `export_bundle` packages a *finished
render* for platform hand-off, this tool emits an *editorial timeline* (FCPXML)
so the cut can be reopened, colour-graded, and finished in DaVinci Resolve or
Final Cut Pro instead of being locked to the flat render.

Resolve imports Final Cut Pro XML (FCPXML) timelines reliably; plain EDL
(CMX3600) has no multi-track or transform support, so side-by-side grid
scenes couldn't be represented at all. FCPXML's "connected clips" (the
`lane` attribute) let multiple simultaneous videos stack on one anchor
clip, which is how grid scenes translate.

Grid cells land on separate connected video tracks (V2, V3, ...) at the
correct timeline position, each with a per-column-count template applied
(see GRID_TEMPLATES in build_fcpxml). Two prior attempts guessing generic
FCPXML <adjust-transform> (position+scale, no crop/conform) failed to
composite correctly in Resolve. The working recipe — <adjust-crop> (trim,
percent) + <adjust-conform type="fit"> + <adjust-transform> with scale
always "1 1" — was reverse-engineered from a timeline the user positioned
by hand in Resolve and exported back out, since Resolve's internal
position unit space isn't the 1920x1080 canvas and its exact conversion
isn't documented anywhere I could find.

Usage (CLI):
    python -m tools.publishers.fcpxml_export <project_dir> [--limit N] [--out PATH]

Usage (tool / registry):
    registry.get("fcpxml_export").execute({"project_dir": "projects/<name>"})
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

FPS = 30
FORMAT_ID = "r1"


def _seconds_to_fcp_time(seconds: float) -> str:
    """Rational time snapped to whole frames at FPS, e.g. '60/30s'."""
    frames = round(seconds * FPS)
    return f"{frames}/{FPS}s"


def _probe_media(path: Path) -> tuple[float, bool]:
    """Return (duration_seconds, has_audio_stream)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=15,
    )
    data = json.loads(proc.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return duration, has_audio


def build_fcpxml(
    scenes: list[dict],
    path_by_filename: dict[str, str],
    music_path: str | None,
    music_duration: float | None,
    project_name: str,
    clip_rotations: dict[str, float] | None = None,
    clip_in_overrides: dict[str, dict[str, float]] | None = None,
) -> str:
    clip_rotations = clip_rotations or {}
    clip_in_overrides = clip_in_overrides or {}
    # --- resources: one asset per unique source file, plus music ---
    asset_ids: dict[str, str] = {}
    resource_xml: list[str] = [
        f'<format id="{FORMAT_ID}" name="FFVideoFormat1080p30" '
        f'frameDuration="1/{FPS}s" width="1920" height="1080"/>'
    ]

    def get_or_create_asset(filename: str) -> str:
        if filename in asset_ids:
            return asset_ids[filename]
        rid = f"r{len(asset_ids) + 2}"
        asset_ids[filename] = rid
        abs_path = path_by_filename[filename]
        p = Path(abs_path)
        dur, has_audio = _probe_media(p)
        name = p.stem
        audio_attrs = (
            'hasAudio="1" audioSources="1" audioChannels="2"' if has_audio else 'hasAudio="0"'
        )
        resource_xml.append(
            f'<asset id="{rid}" name={quoteattr(name)} src={quoteattr(p.as_uri())} '
            f'start="0s" duration="{_seconds_to_fcp_time(dur)}" hasVideo="1" '
            f'format="{FORMAT_ID}" {audio_attrs}/>'
        )
        return rid

    for s in scenes:
        for ra in s["required_assets"]:
            get_or_create_asset(ra["path"])

    music_rid = None
    if music_path:
        music_rid = f"r{len(asset_ids) + 2}"
        p = Path(music_path)
        resource_xml.append(
            f'<asset id="{music_rid}" name={quoteattr(p.stem)} src={quoteattr(p.resolve().as_uri())} '
            f'start="0s" duration="{_seconds_to_fcp_time(music_duration or 0)}" '
            'hasAudio="1" audioSources="1" audioChannels="2" hasVideo="0"/>'
        )

    # --- spine: solo scenes as sequential clips, grid scenes as an anchor
    # clip with the remaining cells attached as connected clips on lanes ---
    #
    # Two prior attempts at guessing FCPXML's generic Motion-style
    # <adjust-transform> (position+scale only) failed to composite in
    # Resolve — a non-uniform scale skewed the video, and a corrected
    # uniform scale sat on an opaque background that hid the layers
    # underneath. The user then positioned one 3-up and one 2-up grid
    # manually in Resolve and exported the timeline, which revealed
    # Resolve's actual recipe: <adjust-crop> (trim, percent per side) +
    # <adjust-conform type="fit"> + <adjust-transform> with scale ALWAYS
    # "1 1" and position doing the real work, in a unit space that isn't
    # the 1920x1080 canvas (position values are roughly canvas/13, likely
    # relative to the conformed/cropped clip's own frame). These are the
    # two verified per-column-count templates — not derived analytically,
    # since Resolve's exact unit conversion is opaque from the outside.
    GRID_TEMPLATES: dict[int, dict[str, object]] = {
        2: {"crop_pct": 0.0, "positions": [-99.63, 99.63]},
        3: {"crop_pct": 16.5, "positions": [-145.6, 0.0, 145.6]},
    }

    spine_items: list[str] = []
    for s in scenes:
        dur = _seconds_to_fcp_time(s["end_seconds"] - s["start_seconds"])
        offset = _seconds_to_fcp_time(s["start_seconds"])
        assets = s["required_assets"]
        n = len(assets)

        if n == 1:
            path = assets[0]["path"]
            rid = asset_ids[path]
            name = Path(path).stem
            start_seconds = clip_in_overrides.get(s["id"], {}).get(path, 0.0)
            start_time = _seconds_to_fcp_time(start_seconds) if start_seconds else "0s"
            rotation = clip_rotations.get(s["id"])
            inner = f'<adjust-transform rotation="{rotation}"/>' if rotation else ""
            if inner:
                spine_items.append(
                    f'<asset-clip ref="{rid}" offset="{offset}" duration="{dur}" '
                    f'start="{start_time}" name={quoteattr(name)}>{inner}</asset-clip>'
                )
            else:
                spine_items.append(
                    f'<asset-clip ref="{rid}" offset="{offset}" duration="{dur}" '
                    f'start="{start_time}" name={quoteattr(name)}/>'
                )
        else:
            template = GRID_TEMPLATES.get(n)

            def cell_adjustments(i: int) -> str:
                if template is None:
                    return ""
                pos = template["positions"][i]
                crop = template["crop_pct"]
                crop_xml = (
                    f'<adjust-crop mode="trim"><trim-rect right="{crop}" left="{crop}"/></adjust-crop>'
                    if crop else ""
                )
                return (
                    f'{crop_xml}<adjust-conform type="fit"/>'
                    f'<adjust-transform position="{pos} 0" anchor="0 0" scale="1 1"/>'
                )

            cell_in_overrides = clip_in_overrides.get(s["id"], {})

            def cell_start(path: str) -> str:
                secs = cell_in_overrides.get(path, 0.0)
                return _seconds_to_fcp_time(secs) if secs else "0s"

            anchor_path = assets[0]["path"]
            anchor_rid = asset_ids[anchor_path]
            anchor_name = Path(anchor_path).stem
            connected: list[str] = []
            for i, ra in enumerate(assets[1:], start=1):
                rid = asset_ids[ra["path"]]
                name = Path(ra["path"]).stem
                # Nested/connected clips' offset is relative to the anchor's
                # own start, not the absolute timeline position — using the
                # scene's absolute offset here double-counted it, causing
                # lanes to lag behind their anchor (worse the later a grid
                # falls in the timeline). Confirmed against the user's own
                # manually-positioned export, which used offset="0/1s" for
                # every connected clip.
                connected.append(
                    f'<asset-clip ref="{rid}" lane="{i}" offset="0s" '
                    f'duration="{dur}" start="{cell_start(ra["path"])}" name={quoteattr(name)}>'
                    f'{cell_adjustments(i)}'
                    f'</asset-clip>'
                )
            spine_items.append(
                f'<asset-clip ref="{anchor_rid}" offset="{offset}" duration="{dur}" '
                f'start="{cell_start(anchor_path)}" name={quoteattr(anchor_name)}>'
                f'{cell_adjustments(0)}'
                + "".join(connected)
                + "</asset-clip>"
            )

    total_dur = _seconds_to_fcp_time(scenes[-1]["end_seconds"] if scenes else 0)

    music_clip = ""
    if music_rid:
        music_clip = (
            f'<asset-clip ref="{music_rid}" lane="-1" offset="0s" '
            f'duration="{_seconds_to_fcp_time(music_duration or 0)}" start="0s" name="music"/>'
        )

    resources = "\n    ".join(resource_xml)
    spine = "\n        ".join(spine_items)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
  <resources>
    {resources}
  </resources>
  <library>
    <event name={quoteattr(project_name)}>
      <project name={quoteattr(project_name)}>
        <sequence format="{FORMAT_ID}" duration="{total_dur}" tcStart="0s" tcFormat="NDF">
          <spine>
            {spine}
            {music_clip}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


def export_project_to_fcpxml(
    project_dir: str | Path,
    out: str | Path | None = None,
    limit: int | None = None,
) -> tuple[Path, int]:
    """Load a project's scene_plan + asset_manifest and write an FCPXML timeline.

    Shared core used by both the CLI (`main`) and the `FcpxmlExport` tool.
    Returns ``(output_path, scene_count)``.
    """
    proj = Path(project_dir)
    scene_plan = json.loads((proj / "artifacts/scene_plan.json").read_text())
    asset_manifest = json.loads((proj / "artifacts/asset_manifest.json").read_text())

    scenes = scene_plan["scenes"]
    if limit:
        scenes = scenes[:limit]

    music_asset = next((a for a in asset_manifest["assets"] if a["type"] == "music"), None)
    music_path = music_asset["path"] if music_asset else None
    music_duration = music_asset.get("duration_seconds") if music_asset else None

    # scene_plan stores bare filenames; asset_manifest has the real absolute
    # paths (the source folder is outside the repo, e.g. ~/Downloads/...).
    path_by_filename = {
        Path(a["path"]).name: a["path"] for a in asset_manifest["assets"] if a["type"] == "video"
    }

    # Dolby Vision (10-bit HEVC profile 8) clips don't decode in Resolve on
    # some machines — route those specific files to their transcoded 8-bit
    # Rec.709 H.264 versions instead when present. See assets/transcoded_sdr/.
    sdr_dir = proj / "assets" / "transcoded_sdr"
    if sdr_dir.exists():
        for filename in list(path_by_filename):
            sdr_path = sdr_dir / f"{Path(filename).stem}.mp4"
            if sdr_path.exists():
                path_by_filename[filename] = str(sdr_path.resolve())

    clip_rotations = scene_plan.get("metadata", {}).get("clip_rotations", {})
    clip_in_overrides = scene_plan.get("metadata", {}).get("clip_in_overrides", {})

    xml = build_fcpxml(
        scenes, path_by_filename, music_path, music_duration, project_name=proj.name,
        clip_rotations=clip_rotations, clip_in_overrides=clip_in_overrides,
    )

    out_path = Path(out) if out else proj / "renders" / f"{proj.name}{'_test' if limit else ''}.fcpxml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(xml)
    return out_path, len(scenes)


class FcpxmlExport(BaseTool):
    name = "fcpxml_export"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "fcpxml"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffprobe"]  # reads clip durations / audio streams
    install_instructions = (
        "Requires the FFmpeg suite (provides ffprobe). Install via "
        "'brew install ffmpeg' (macOS) or 'apt-get install ffmpeg' (Linux)."
    )

    agent_skills = []

    capabilities = ["export_fcpxml", "grid_timeline"]
    supports = {
        "local_offline": True,
        "free": True,
        "uploads": False,
        "multi_track_grid": True,
    }
    best_for = [
        "handing a cut to DaVinci Resolve / Final Cut Pro for colour + finishing",
        "preserving side-by-side grid scenes as multi-track connected clips",
        "an editable editorial timeline instead of a locked flat render",
    ]
    not_good_for = [
        "packaging a finished render for platform upload (use export_bundle)",
        "timelines with grid scenes wider than 3 columns (only 2-up/3-up templates verified)",
    ]

    input_schema = {
        "type": "object",
        "required": ["project_dir"],
        "properties": {
            "project_dir": {
                "type": "string",
                "description": "Project workspace containing artifacts/scene_plan.json and artifacts/asset_manifest.json.",
            },
            "out": {
                "type": "string",
                "description": "Override the output path. Defaults to <project_dir>/renders/<name>.fcpxml.",
            },
            "limit": {
                "type": "integer",
                "description": "Only export the first N scenes (for a quick test import into Resolve).",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "fcpxml_path": {"type": "string"},
            "scene_count": {"type": "integer"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=0, network_required=False
    )
    side_effects = ["writes an .fcpxml timeline file to disk"]
    user_visible_verification = [
        "Import the .fcpxml into DaVinci Resolve and confirm clips, grid layout, and music land correctly",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        project_dir = Path(inputs["project_dir"]).expanduser()
        if not project_dir.is_dir():
            return ToolResult(success=False, error=f"project_dir not found: {project_dir}")

        scene_plan = project_dir / "artifacts" / "scene_plan.json"
        asset_manifest = project_dir / "artifacts" / "asset_manifest.json"
        for required in (scene_plan, asset_manifest):
            if not required.is_file():
                return ToolResult(
                    success=False,
                    error=f"required artifact missing: {required}",
                )

        try:
            out_path, scene_count = export_project_to_fcpxml(
                project_dir, out=inputs.get("out"), limit=inputs.get("limit")
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"FCPXML export failed: {exc}")

        return ToolResult(
            success=True,
            data={"fcpxml_path": str(out_path), "scene_count": scene_count},
            artifacts=[str(out_path)],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a project cut to FCPXML for DaVinci Resolve.")
    parser.add_argument("project_dir")
    parser.add_argument("--limit", type=int, default=None, help="Only export the first N scenes (for a test import)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out_path, n_scenes = export_project_to_fcpxml(args.project_dir, out=args.out, limit=args.limit)
    print(f"wrote {out_path} ({n_scenes} scenes)")


if __name__ == "__main__":
    main()

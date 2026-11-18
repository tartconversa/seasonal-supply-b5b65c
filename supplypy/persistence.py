"""运行归档与可重现性：保存输入摘要、参数、随机种子、结果与依赖记录。"""
from __future__ import annotations

import hashlib
import json
import platform
import datetime as dt
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .dataio import DataBundle


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_fingerprint(bundle: DataBundle) -> dict[str, str]:
    """资料包内每张表的摘要，复算时核对输入未被替换。"""
    out = {}
    for table in bundle.catalog["tables"]:
        p = bundle.root / table["path"]
        out[table["name"]] = sha256_file(p)
    return out


def to_native(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_native(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return obj


def save_run(
    out_dir: str | Path,
    run_name: str,
    params: dict,
    bundle: DataBundle,
    report: Any | None = None,
    extra: dict | None = None,
) -> Path:
    """写入 manifest、参数与结果。目录名带时间戳，避免覆盖历史签约依据。"""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(out_dir) / f"{run_name}-{stamp}"
    path.mkdir(parents=True, exist_ok=False)

    manifest = {
        "run_name": run_name,
        "created_at": stamp,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "input_fingerprint": input_fingerprint(bundle),
        "params": to_native(params),
        "artifacts": [],
    }
    if report is not None:
        report_path = path / "report.json"
        report_path.write_text(
            json.dumps(to_native(report), ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["artifacts"].append("report.json")
    if extra:
        for name, payload in extra.items():
            fp = path / f"{name}.json"
            fp.write_text(json.dumps(to_native(payload), ensure_ascii=False, indent=2),
                          encoding="utf-8")
            manifest["artifacts"].append(f"{name}.json")
    (path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_manifest(run_dir: str | Path) -> dict:
    return json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))

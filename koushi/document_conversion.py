# -*- coding: utf-8 -*-
"""舊式 Word 範本的無人值守轉換。

官方附件4只有二進位 ``.doc``。產生器不修改來源，而是在隔離的暫存目錄
以 LibreOffice headless 轉成 ``.docx``，供 python-docx 套版；暫存檔在工作
結束後自動移除。
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from . import config as C


def find_soffice() -> Path | None:
    """尋找可用的 LibreOffice 命令列程式，不啟動 GUI。"""
    env_path = os.environ.get("KOUSHI_SOFFICE", "").strip()
    candidates = [
        Path(env_path) if env_path else None,
        C.ROOT.parent / ".oral-exam-agent-cache" / "libreoffice-runtime" / "program" / "soffice.com",
        C.ROOT / "tmp" / "libreoffice-cache" / "libreoffice-runtime" / "program" / "soffice.com",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "LibreOffice" / "program" / "soffice.com",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "LibreOffice" / "program" / "soffice.com",
    ]
    for command in ("soffice.com", "soffice", "libreoffice"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    return next((path for path in candidates if path and path.is_file()), None)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "check": True,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 180,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


@contextmanager
def docx_template(path: str | Path, *, soffice: str | Path | None = None):
    """將 ``.doc`` 暫時轉成 ``.docx``；``.docx`` 則直接沿用。

    所有轉換都發生在系統暫存目錄，來源範本保持唯讀。找不到轉檔器、轉換
    失敗或逾時時直接中止，不能留下半套附件。
    """
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".docx":
        yield source
        return
    if suffix != ".doc":
        raise ValueError(f"附件4範本只接受 .doc 或 .docx：{source}")

    executable = Path(soffice) if soffice else find_soffice()
    if executable is None:
        raise RuntimeError(
            "附件4是舊式 .doc，但找不到 LibreOffice。"
            "請安裝 LibreOffice 或設定 KOUSHI_SOFFICE；未產生任何檔案。"
        )

    with TemporaryDirectory(prefix="koushi-att4-") as folder:
        work = Path(folder)
        input_path = work / "input.doc"
        output_dir = work / "output"
        profile_dir = work / "profile"
        output_dir.mkdir()
        profile_dir.mkdir()
        shutil.copy2(source, input_path)

        profile_uri = profile_dir.resolve().as_uri()
        common = [
            str(executable),
            "--headless",
            "--invisible",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--nolockcheck",
            "--norestore",
            f"-env:UserInstallation={profile_uri}",
        ]
        try:
            _run([*common, "--terminate_after_init"])
            result = _run([
                *common,
                "--infilter=MS Word 97",
                "--convert-to",
                "docx:Office Open XML Text",
                "--outdir",
                str(output_dir),
                str(input_path),
            ])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"附件4 .doc 自動轉檔失敗；未產生任何檔案：{exc}"
            ) from exc

        converted = output_dir / "input.docx"
        if not converted.is_file():
            detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            raise RuntimeError(
                "附件4 .doc 自動轉檔未產生 DOCX；未產生任何檔案。"
                + (f"\n{detail}" if detail else "")
            )
        yield converted

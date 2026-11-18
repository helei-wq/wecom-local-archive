#!/usr/bin/env python3
"""Build a local HTML archive and JSONL from saved WeCom Markdown exports."""

import argparse
from dataclasses import dataclass, field
from datetime import date, time
from html import escape
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from urllib.parse import unquote, urlsplit


DOCUMENT = "聊天记录.md"
BASELINE_PREFIX = "全量-截至-"
DATE_STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
DAY_HEADING = re.compile(rf"## ({DATE_STAMP})")
EXPORT_NAME = re.compile(rf"({BASELINE_PREFIX})?({DATE_STAMP})(?:\.md)?")
MESSAGE_HEADING = re.compile(r"\*\*([0-9]{2}:[0-9]{2}:[0-9]{2}) · (.*?)\*\* · (.+)")
MEDIA_LINK = re.compile(r"^!\[(?P<image>[^\]]*)\]\((?P<image_path>[^()\s]+)\)$|"
                        r"^- .+ · \[(?P<file>[^\]]*)\]\((?P<file_path>[^()\s]+)\)$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@dataclass
class Attachment:
    name: str
    source: Path | None
    is_image: bool


@dataclass
class Message:
    day: date
    time: time
    sender: str
    message_type: str
    body: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


def select_documents(source):
    if source.is_file():
        return [(source, None, False)]
    direct = source / DOCUMENT
    if direct.is_file():
        return [(direct, None, False)]
    candidates = {}
    for item in sorted(source.iterdir()):
        match = EXPORT_NAME.fullmatch(item.name)
        if match is None:
            continue
        cutoff = date.fromisoformat(match[2])
        role = bool(match[1])
        if item.is_dir() and (item / DOCUMENT).is_file():
            candidates[(role, cutoff)] = item / DOCUMENT
        elif item.is_file() and item.suffix == ".md":
            candidates.setdefault((role, cutoff), item)
    latest = max((cutoff for baseline, cutoff in candidates if baseline), default=None)
    choice = [(candidates[(True, latest)], latest, True)] if latest is not None else []
    choice.extend((p, when, False) for (baseline, when), p in sorted(candidates.items())
                  if not baseline and (latest is None or when > latest))
    if not choice:
        raise ValueError("没有找到聊天记录 Markdown、全量基线或按日导出")
    return choice


def resolve_attachment(source_file, raw_path):
    try:
        url = urlsplit(raw_path)
        if url.scheme or url.netloc or url.query or url.fragment:
            return None
        parts = PurePosixPath(unquote(url.path))
        if not parts.parts or parts.parts[0] != "附件" or ".." in parts.parts:
            return None
        root = source_file.parent.resolve()
        path = root.joinpath(*parts.parts).resolve(strict=True)
        if path.is_relative_to(root / "附件") and path.is_file():
            return path
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def read_document(source_file):
    lines = source_file.read_text(encoding="utf-8-sig").splitlines()
    if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
        raise ValueError(f"聊天记录缺少群名标题：{source_file.name}")
    group = lines[0][2:].strip()
    day = None
    current = None
    messages = []
    for line in lines[1:]:
        heading = DAY_HEADING.fullmatch(line)
        if heading:
            day = date.fromisoformat(heading[1])
            current = None
            continue
        metadata = MESSAGE_HEADING.fullmatch(line)
        if metadata and day is not None:
            current = Message(day, time.fromisoformat(metadata[1]), metadata[2], metadata[3])
            messages.append(current)
            continue
        if current is None:
            continue
        if line.startswith("    "):
            current.body.append(line[4:])
            continue
        linked = MEDIA_LINK.fullmatch(line.strip())
        if linked:
            image = linked["image_path"] is not None
            label = linked["image"] if image else linked["file"]
            raw_path = linked["image_path"] if image else linked["file_path"]
            current.attachments.append(Attachment(label, resolve_attachment(source_file, raw_path), image))
    total = next((re.fullmatch(r"本机数据库记录 ([0-9]+) 条。?", line) for line in lines[1:]
                  if line.startswith("本机数据库记录 ")), None)
    if total is not None and int(total[1]) != len(messages):
        raise ValueError(f"声明的条数与解析结果不一致：{source_file.name}")
    return group, messages


def load_archive(source, start, through):
    group = None
    archive = []
    for document, boundary, is_baseline in select_documents(source):
        name, messages = read_document(document)
        if group is not None and group != name:
            raise ValueError("这些文件包含多个群聊，请每次输入一个群的归档")
        group = name
        for message in messages:
            if boundary is not None and (message.day > boundary if is_baseline else message.day != boundary):
                raise ValueError(f"消息日期与导出文件名不一致：{document}")
            if (start is None or message.day >= start) and (through is None or message.day <= through):
                archive.append(message)
    return group, sorted(archive, key=lambda item: (item.day, item.time))


def write_archive(destination, group, messages):
    if destination.exists():
        raise FileExistsError(f"输出目录已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wecom-archive-", dir=destination.parent) as directory:
        folder = Path(directory) / "archive"
        folder.mkdir(mode=0o700)
        (folder / "attachments").mkdir()
        copied = {}
        day = None
        with (folder / "archive.html").open("w", encoding="utf-8") as page, \
                (folder / "messages.jsonl").open("w", encoding="utf-8") as records:
            page.write("""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {font: 16px/1.55 system-ui, sans-serif; max-width: 54rem; margin: 2rem auto;
padding: 0 1rem; color: #20252b; background: #fff}
h2 {border-bottom: 1px solid #d8dfe6; padding-bottom: .35rem}
article {margin: 1rem 0 1.5rem}
header, figcaption {color: #52606d}
.message {white-space: pre-wrap; overflow-wrap: anywhere}
figure {margin: .5rem 0}
img {display: block; max-width: 100%; height: auto}
</style>
""")
            page.write(f"<title>{escape(group)}</title><main><h1>{escape(group)}</h1>\n")
            page.write(f"<p>{len(messages)} 条消息</p>\n")
            for message in messages:
                if message.day != day:
                    day = message.day
                    page.write(f"<h2>{day}</h2>\n")
                time_text = message.time.isoformat()
                header = f"{time_text} · {message.sender} · {message.message_type}"
                body = "\n".join(message.body)
                page.write(f'<article><header>{escape(header)}</header><p class="message">{escape(body)}</p>\n')
                attachments = []
                for attachment in message.attachments:
                    label = escape(attachment.name)
                    source = attachment.source
                    href = None
                    if source is not None:
                        if source not in copied:
                            suffix = source.suffix.lower()
                            suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin"
                            href = f"attachments/{len(copied) + 1:05d}{suffix}"
                            shutil.copyfile(source, folder / href)
                            copied[source] = href
                        href = copied[source]
                        if attachment.is_image and source.suffix.lower() in IMAGE_SUFFIXES:
                            page.write(f'<figure><a href="{href}"><img src="{href}" alt="{label}" loading="lazy"></a>'
                                       f'<figcaption>{label}</figcaption></figure>\n')
                        else:
                            page.write(f'<p><a href="{href}" download>{label}</a></p>\n')
                    else:
                        page.write(f"<p>{label} · 本机没有可用文件</p>\n")
                    attachments.append({"name": attachment.name, "path": href, "is_image": attachment.is_image})
                record = {"group": group, "date": day.isoformat(), "time": time_text,
                          "sender": message.sender, "type": message.message_type, "text": body,
                          "attachments": attachments}
                records.write(json.dumps(record, ensure_ascii=False) + "\n")
                page.write("</article>\n")
            page.write("</main></html>\n")
        if destination.exists():
            raise FileExistsError(f"输出目录已存在：{destination}")
        folder.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="已导出的 Markdown 文件或单群归档目录")
    parser.add_argument("--output", type=Path, required=True, help="新的输出目录")
    parser.add_argument("--start", type=date.fromisoformat, help="起始日期，包含当天")
    parser.add_argument("--through", type=date.fromisoformat, help="截止日期，包含当天")
    args = parser.parse_args()
    if args.start and args.through and args.start > args.through:
        parser.error("起始日期晚于截止日期")
    try:
        source = args.input.expanduser().resolve(strict=True)
        group, messages = load_archive(source, args.start, args.through)
        output = args.output.expanduser().absolute()
        write_archive(output, group, messages)
    except (OSError, ValueError) as error:
        print(f"整理失败：{error}", file=sys.stderr)
        return 1
    print(f"已整理 {len(messages)} 条消息。打开 {output / 'archive.html'} 浏览。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

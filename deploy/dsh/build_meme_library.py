#!/usr/bin/env python3
"""Build Whale-chan's community meme library from 梗鲸 (aigengtu.com).

The site curates fan-made DeepSeek-chan memes, each with its line of dialogue and a short story; the
author allows non-commercial fan use and hotlinking. This downloads the ORIGINAL image of each
「梗图」 entry and writes index.json, which the social runtime (meme-library.mjs) searches when she
writes [梗圖:…] in a reply. Re-running only fetches what is new.

- The format is read from the file's first bytes, not from the URL: the site's extensions are
  sometimes wrong (a ".png" that is a JPEG, a ".jpeg" that is a GIF).
- Originals are kept at full size. Discord rejects attachments over 10 MB, so meme-library.mjs
  compresses a large one when it sends it; for those the preview is saved too (index "fallback"), in
  case compression fails.
- Only if the original can't be downloaded does the entry use the site's compressed WebP preview.

Run on the VM as the dsh user:
  sudo -u dsh python3 build_meme_library.py [/var/lib/dsh/.dsh/meme-library/aigengtu]
"""

import glob
import json
import os
import re
import sys
import tempfile
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

INDEX_URL = "https://aigengtu.com/gallery-index.json"
CATEGORY = "DeepSeek娘"
USER_AGENT = "Mozilla/5.0 (compatible; whale-chan meme library; personal non-commercial bot)"
MAX_DOWNLOAD = 256 * 1024 * 1024
# Discord's attachment limit for bots on a server without boosts (meme-library.mjs uses the same).
DISCORD_LIMIT = 10 * 1000 * 1000
ORIGINAL_DIR = "img-original"
PREVIEW_DIR = "img"


def fetch(url, limit=MAX_DOWNLOAD):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data


def sniff(data):
    """Real image type from the first bytes: jpg, png, gif, webp, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def entry(image):
    number = re.search(r"/meme/(\d+)\.\w+$", image.get("original", "") or image.get("preview", ""))
    if not number or image.get("type") != "梗图":
        return None
    alt = image.get("alt") or image.get("name") or ""
    quote = re.search(r"『([^』]+)』", alt)
    # 「DeepSeek娘『台词』动作描述表情包」：去掉前面的角色名与结尾的「表情包」，留下台词与描述。
    caption = re.sub(r"^(?:DeepSeek(?:娘|鲸娘)?|蓝色大肥鱼|大肥鱼)\s*", "", alt)
    caption = re.sub(r"表情包$", "", caption).strip()
    story = ((image.get("story") or {}).get("zh") or "").split("。")[0][:120]
    return {
        "n": number.group(1),
        "quote": quote.group(1).strip() if quote else "",
        "caption": caption[:120],
        "story": story,
        "original": image.get("original") or "",
        "preview": image.get("preview") or "",
    }


def existing(root, folder, n):
    """A file already built for this meme in folder, as a path relative to root, or None."""
    for path in sorted(glob.glob(os.path.join(root, folder, n + ".*"))):
        if path.rsplit(".", 1)[1] in ("jpg", "png", "gif", "webp"):
            return os.path.relpath(path, root).replace(os.sep, "/")
    return None


def write(root, relative, data):
    path = os.path.join(root, relative)
    with open(path + ".part", "wb") as file:
        file.write(data)
    os.replace(path + ".part", path)


def preview(root, meme, fetcher):
    """The site's WebP preview, downloaded once. Returns the relative path or None."""
    done = existing(root, PREVIEW_DIR, meme["n"])
    if done:
        return done
    try:
        data = fetcher(meme["preview"])
        kind = sniff(data)
        if not kind or len(data) > DISCORD_LIMIT:
            raise ValueError("bad preview")
    except Exception as error:  # noqa: BLE001
        print("no preview", meme["n"], error, file=sys.stderr)
        return None
    relative = f"{PREVIEW_DIR}/{meme['n']}.{kind}"
    write(root, relative, data)
    return relative


def build_one(root, meme, fetcher=fetch):
    """Download one meme. Returns (file, source, fallback): file relative to root; source is
    original or preview (None if nothing usable); fallback is the preview for originals over
    Discord's limit, else None."""
    n = meme["n"]
    # An earlier build compressed large originals; those must be fetched again at full size.
    marker = os.path.join(root, ORIGINAL_DIR, n + ".compressed")
    if os.path.exists(marker):
        for path in glob.glob(os.path.join(root, ORIGINAL_DIR, n + ".*")):
            os.remove(path)
    done = existing(root, ORIGINAL_DIR, n)
    if not done:
        try:
            data = fetcher(meme["original"]) if meme["original"] else None
            kind = sniff(data) if data else None
            if not kind:
                raise ValueError("no original" if not data else "unknown format")
            done = f"{ORIGINAL_DIR}/{n}.{kind}"
            write(root, done, data)
        except Exception as error:  # noqa: BLE001 - one bad image only drops back to the preview
            print("original failed", n, error, file=sys.stderr)
    if done:
        large = os.path.getsize(os.path.join(root, done)) > DISCORD_LIMIT
        return done, "original", preview(root, meme, fetcher) if large else None
    fallback = preview(root, meme, fetcher)
    return (fallback, "preview", None) if fallback else (None, None, None)


def main(root):
    os.makedirs(os.path.join(root, ORIGINAL_DIR), exist_ok=True)
    os.makedirs(os.path.join(root, PREVIEW_DIR), exist_ok=True)
    gallery = json.loads(fetch(INDEX_URL))
    memes = [m for m in (entry(i) for i in gallery["gallery"][CATEGORY]["images"]) if m]

    with ThreadPoolExecutor(max_workers=4) as pool:
        built = list(pool.map(lambda meme: build_one(root, meme), memes))
    kept = []
    for meme, (relative, source, fallback) in zip(memes, built):
        if relative:
            item = {"n": meme["n"], "file": relative, "source": source, "quote": meme["quote"],
                    "caption": meme["caption"], "story": meme["story"]}
            if fallback:
                item["fallback"] = fallback
            kept.append(item)
    index = {
        "source": "梗鲸 GengJing https://aigengtu.com/ (fan-made, non-commercial use)",
        "generated_at": gallery.get("generated_at"),
        "count": len(kept),
        "memes": kept,
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, delete=False) as file:
        json.dump(index, file, ensure_ascii=False)
        temporary = file.name
    os.chmod(temporary, 0o644)
    os.replace(temporary, os.path.join(root, "index.json"))
    print(json.dumps({
        "memes": len(kept),
        "skipped": len(memes) - len(kept),
        "sources": Counter(m["source"] for m in kept),
        "formats": Counter(m["file"].rsplit(".", 1)[1] for m in kept),
        "over_discord_limit": sum(1 for m in kept if m.get("fallback")),
    }))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/var/lib/dsh/.dsh/meme-library/aigengtu")

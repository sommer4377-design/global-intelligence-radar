"""下载 Argos Translate 的公开模型到指定目录。"""
import argparse
from pathlib import Path
from shutil import copyfileobj
import subprocess
from tempfile import TemporaryDirectory
import zipfile


def download(pair, destination):
    model = f"translate-{pair}-1_9"
    with TemporaryDirectory() as scratch:
        archive = Path(scratch) / "model.zip"
        subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
             "--proto-redir", "=https", "--max-time", "180", "--output", str(archive),
             f"https://argos-net.com/v1/{model}.argosmodel"], check=True,
        )
        with zipfile.ZipFile(archive) as package:
            root = Path(model)
            tokenizer = "sentencepiece.model" if pair != "es_en" else "bpe.model"
            required = ("metadata.json", tokenizer, "README.md",
                        "model/config.json", "model/shared_vocabulary.json", "model/model.bin")
            for filename in required:
                relative = root / filename
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(relative.as_posix()) as source, target.open("wb") as output:
                    copyfileobj(source, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--spanish", action="store_true")
    args = parser.parse_args()
    for pair in ["en_zh", "pt_en"] + (["es_en"] if args.spanish else []):
        download(pair, args.directory)

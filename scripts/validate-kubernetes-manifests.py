#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def validate_directory(root: Path, excluded_names: set[str]) -> tuple[int, int]:
    paths = sorted(
        path
        for path in root.glob("*.yaml")
        if path.name not in excluded_names
    )
    if not paths:
        raise ValueError(f"no Kubernetes manifests found in {root}")

    document_count = 0
    for path in paths:
        documents = [
            document
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if document is not None
        ]
        if not documents:
            raise ValueError(f"{path}: no YAML documents")
        for index, document in enumerate(documents, start=1):
            validate_document(path, index, document)
            document_count += 1

    return len(paths), document_count


def validate_document(path: Path, index: int, document: Any) -> None:
    if not isinstance(document, dict):
        raise TypeError(f"{path} document {index}: expected a mapping")
    for field in ("apiVersion", "kind", "metadata"):
        if field not in document:
            raise ValueError(f"{path} document {index}: missing {field}")
    metadata = document["metadata"]
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise ValueError(f"{path} document {index}: missing metadata.name")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse Kubernetes YAML without contacting a cluster."
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    arguments = parser.parse_args()

    file_count, document_count = validate_directory(
        arguments.directory, set(arguments.exclude)
    )
    print(
        f"Validated {document_count} Kubernetes documents from {file_count} "
        "files without contacting the cluster"
    )


if __name__ == "__main__":
    main()

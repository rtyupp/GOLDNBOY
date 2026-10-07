"""سياق آمن ومحدود للمساعد: خريطة المشروع + الحالة الحالية، بلا أسرار بيئة."""
from __future__ import annotations

import os
from pathlib import Path


class ProjectContext:
    def __init__(self, root: str = "."):
        self.root = Path(root)

    def code_map(self) -> str:
        parts = []
        for path in sorted((self.root / "goldbot").rglob("*.py")):
            rel = path.relative_to(self.root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            doc = ""
            for line in text.splitlines()[:8]:
                if line.strip().startswith(('"""', "'''")):
                    doc = line.strip().strip('"\'')[:120]
                    break
            parts.append(f"- {rel}: {doc or 'وحدة بايثون'}")
        return "\n".join(parts)

    def prompt(self, market_state: str = "", news: str = "") -> str:
        readme = ""
        try:
            readme = (self.root / "README.md").read_text(encoding="utf-8")[:2500]
        except OSError:
            pass
        return ("خريطة كود GOLDNBOY (لا تحتوي أسرارًا):\n" + self.code_map()[:7000] +
                "\n\nمقتطف توثيق المشروع:\n" + readme +
                "\n\nحالة السوق الحالية:\n" + (market_state or "غير متاحة") +
                "\n\nآخر الأخبار الحية:\n" + (news or "غير متاحة"))

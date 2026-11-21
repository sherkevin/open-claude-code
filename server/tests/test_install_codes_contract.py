"""Keep the installation event code allowlists compatible across components."""
import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


def block(path, pattern):
    content = (ROOT / path).read_text(encoding="utf-8-sig")
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        raise AssertionError(f"Cannot locate installation codes in {path}")
    return match.group(1)


class InstallCodeContract(unittest.TestCase):
    def test_install_codes(self):
        server = ROOT / "server/app/kaiwu_diagnostics.py"
        values = [
            ast.literal_eval(node.value)
            for node in ast.parse(server.read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "INSTALL_CODES" for target in node.targets)
        ]
        self.assertEqual(len(values), 1)
        accepted = values[0]
        self.assertIn("file_missing", accepted)
        self.assertIn("unknown", accepted)

        allowlists = (
            ("connector/packaging/windows/install.ps1", r"\$Code -cnotin @\(([^)]+)\)"),
            ("connector/packaging/windows/attempt-journal.c", r"\bcodes\[\]\s*=\s*\{([^}]+)\}"),
            ("connector/internal/diagnostics/install_journal.go", r"\binstallCodes\s*=\s*map\[string\]bool\{([^}]+)\}"),
            ("connector/scripts/collect-install-support.ps1", r"\$codes\s*=\s*@\(([^)]+)\)"),
        )
        for path, pattern in allowlists:
            with self.subTest(path=path):
                source = block(path, pattern)
                codes = re.findall(r"""["']([a-z_]+)["']""", source)
                self.assertCountEqual(codes, accepted)

        labels = block("connector/internal/web/page.go", r"const installCodeLabels\s*=\s*\{([^}]+)\}")
        ui_codes = re.findall(r'\b([a-z_]+)\s*:\s*"[^"]+"', labels)
        # Successful events already display their outcome without an extra reason.
        self.assertCountEqual(ui_codes, accepted - {"ok"})


if __name__ == "__main__":
    unittest.main()

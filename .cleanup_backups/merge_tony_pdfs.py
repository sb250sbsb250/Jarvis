"""Merge PDFs in 瓷金发票TONY folder by filename order."""
import os
from pathlib import Path

try:
    from PyPDF2 import PdfMerger
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "PyPDF2", "-q"])
    from PyPDF2 import PdfMerger

src_dir = Path(r"C:\Users\xinzh\Downloads\瓷金发票TONY")
pdf_files = sorted(
    [f for f in src_dir.glob("*.pdf")],
    key=lambda p: p.name
)

print("Files to merge (sorted):")
for i, f in enumerate(pdf_files, 1):
    print(f"  {i}. {f.name}")

output = src_dir / "瓷金发票TONY_合并.pdf"
merger = PdfMerger()

for pdf in pdf_files:
    merger.append(str(pdf))

merger.write(str(output))
merger.close()

print(f"\n✅ Merged {len(pdf_files)} PDFs → {output.name}")
print(f"   Size: {output.stat().st_size:,} bytes")

from pathlib import Path
from zipfile import ZipFile

from lxml import etree


REFERENCE = Path(r"C:\Users\Sanchive Kumar\Downloads\Contractsentry one page report.docx")
FINAL = Path(r"C:\Users\Sanchive Kumar\robot\AI Museum Welcome Robot Showcase Card.docx")
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def document_xml(path):
    with ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def canonical_nodes(root, xpath):
    return [etree.tostring(node, method="c14n") for node in root.xpath(xpath, namespaces=NS)]


reference = document_xml(REFERENCE)
final = document_xml(FINAL)

checks = {
    "paragraph_properties": canonical_nodes(reference, ".//w:pPr") == canonical_nodes(final, ".//w:pPr"),
    "table_properties": canonical_nodes(reference, ".//w:tblPr") == canonical_nodes(final, ".//w:tblPr"),
    "table_grids": canonical_nodes(reference, ".//w:tblGrid") == canonical_nodes(final, ".//w:tblGrid"),
    "cell_properties": canonical_nodes(reference, ".//w:tcPr") == canonical_nodes(final, ".//w:tcPr"),
    "section_properties": canonical_nodes(reference, ".//w:sectPr") == canonical_nodes(final, ".//w:sectPr"),
}

for name, result in checks.items():
    print(f"{name}: {result}")

if not all(checks.values()):
    raise SystemExit(1)

from pathlib import Path
from zipfile import ZipFile

from lxml import etree


SOURCE = Path(r"C:\Users\Sanchive Kumar\Downloads\Contractsentry one page report.docx")
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}

with ZipFile(SOURCE) as archive:
    document = etree.fromstring(archive.read("word/document.xml"))
    relationships = etree.fromstring(archive.read("word/_rels/document.xml.rels"))

targets = {
    rel.get("Id"): rel.get("Target")
    for rel in relationships
}

for blip in document.xpath(".//a:blip", namespaces=NS):
    relation_id = blip.get(f"{{{NS['r']}}}embed")
    parent = blip
    while parent is not None and parent.tag != f"{{{NS['w']}}}p":
        parent = parent.getparent()
    text = "" if parent is None else "".join(parent.xpath(".//w:t/text()", namespaces=NS))
    print(relation_id, targets[relation_id], "paragraph text:", repr(text))
    print(document.getroottree().getpath(parent))

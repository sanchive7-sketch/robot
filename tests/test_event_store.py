import json
from pathlib import Path

from app.event_store import EventStore


def test_event_data_is_in_grounding_prompt(tmp_path: Path) -> None:
    event_file = tmp_path / "event.yaml"
    event_file.write_text(
        "event:\n  name: Test Museum\nprojects:\n  - name: Robot Arm\n",
        encoding="utf-8",
    )
    store = EventStore(event_file)

    prompt = store.system_prompt()

    assert store.event_name() == "Test Museum"
    assert "Robot Arm" in prompt
    assert "Answer ONLY from the EVENT DATA" in prompt
    assert "Never invent" in prompt


def test_reload_applies_event_updates(tmp_path: Path) -> None:
    event_file = tmp_path / "event.yaml"
    event_file.write_text("event:\n  name: First\n", encoding="utf-8")
    store = EventStore(event_file)
    event_file.write_text("event:\n  name: Second\n", encoding="utf-8")

    store.reload()

    assert store.event_name() == "Second"


def test_catalog_is_loaded_and_schema_typo_is_normalized(tmp_path: Path) -> None:
    event_file = tmp_path / "event.yaml"
    catalog_file = tmp_path / "project_catalog.json"
    event_file.write_text("event:\n  name: Test Museum\n", encoding="utf-8")
    catalog_file.write_text(
        json.dumps(
            {
                "catalog_version": "test",
                "projects": [
                    {
                        "project_id": "detector",
                        "oAicial_name": "AI Detector",
                        "zone": {"number": "leave these.", "name": "Vision"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = EventStore(event_file, catalog_file)

    project = store.catalog["projects"][0]
    assert project["official_name"] == "AI Detector"
    assert project["zone"]["number"] == ""
    assert store.project_count() == 1
    assert "AI Detector" in store.system_prompt()


def test_question_context_keeps_index_and_selects_relevant_project(
    tmp_path: Path,
) -> None:
    event_file = tmp_path / "event.yaml"
    catalog_file = tmp_path / "project_catalog.json"
    event_file.write_text("event:\n  name: Test Museum\n", encoding="utf-8")
    catalog_file.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "project_id": "medicine",
                        "official_name": "Medicine Dispenser",
                        "purpose": "Schedules and dispenses tablets.",
                        "aliases": ["Pill Box"],
                        "zone": {"number": "A1", "name": "Health"},
                    },
                    {
                        "project_id": "painting",
                        "official_name": "AI Painting",
                        "purpose": "Generates artwork from prompts.",
                        "visible_features": ["Canvas printer"],
                        "zone": {"number": "B1", "name": "Creative"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    store = EventStore(event_file, catalog_file)

    context = store.context_text("How does the pill box dispense medicine?")

    assert "Medicine Dispenser" in context
    assert "Schedules and dispenses tablets" in context
    assert "AI Painting" in context  # Complete project index is retained.
    assert "Generates artwork from prompts" in context
    assert "Canvas printer" not in context

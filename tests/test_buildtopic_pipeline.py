from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

from src.buildtopic.pipeline import BuildTopicRunner


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def prompt_payload(prompt: str) -> dict[str, object]:
    match = re.search(r"输入：\s*(\{.*\})\s*$", prompt, flags=re.DOTALL)
    assert match is not None
    payload = json.loads(match.group(1))
    assert isinstance(payload, dict)
    return payload


class RecordingLLM:
    def __init__(self) -> None:
        self.single_turn_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def single_turn(self, prompt: str, **_: object) -> str:
        self.single_turn_calls.append(prompt)
        if '"stage": "entity_alignment"' in prompt:
            return json.dumps(
                {
                    "alignment_analysis": {
                        "summary": "Entity definitions are aligned by abstract parent semantics.",
                        "parent_alignment_decisions": [],
                        "split_decisions": [],
                        "uncertain_points": [],
                    },
                    "canonical_entities": [
                        {
                            "entity_id": "E_PERSON",
                            "name": "人员",
                            "aliases": ["居民"],
                            "local_refs": [
                                {"question_id": "q1", "local_name": "人员"},
                                {"question_id": "q2", "local_name": "居民"},
                            ],
                        },
                        {
                            "entity_id": "E_AREA",
                            "name": "行政区域",
                            "aliases": ["街道"],
                            "local_refs": [
                                {"question_id": "q1", "local_name": "行政区域"},
                                {"question_id": "q2", "local_name": "街道"},
                            ],
                        },
                    ],
                    "canonical_attributes": [
                        {
                            "attribute_id": "A_PERSON_CLASS",
                            "owner_type": "entity",
                            "owner_id": "E_PERSON",
                            "name": "人员类别",
                            "semantics": "按父类对齐后保留的人员子类或人口口径。",
                            "aliases": ["居民类别"],
                            "local_refs": [
                                {
                                    "question_id": "q2",
                                    "local_name": "居民",
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        if '"stage": "relation_alignment"' in prompt:
            assert '"entity_id": "E_PERSON"' in prompt
            assert '"entity_id": "E_AREA"' in prompt
            return json.dumps(
                {
                    "alignment_analysis": {
                        "summary": "Relation definitions are aligned by predicate semantics.",
                        "merge_decisions": [],
                        "split_decisions": [],
                        "uncertain_points": [],
                    },
                    "canonical_relations": [
                        {
                            "relation_id": "R_RESIDENCE",
                            "name": "居住关系",
                            "participants": [
                                {"entity_id": "E_PERSON", "role": "居民"},
                                {"entity_id": "E_AREA", "role": "居住地"},
                            ],
                            "local_refs": [
                                {
                                    "question_id": "q1",
                                    "local_name": "居住关系",
                                    "participant_entity_ids": ["E_PERSON", "E_AREA"],
                                },
                                {
                                    "question_id": "q2",
                                    "local_name": "居住",
                                    "participant_entity_ids": ["E_PERSON", "E_AREA"],
                                },
                            ],
                        }
                    ],
                    "canonical_attributes": [
                        {
                            "attribute_id": "A_REL_DUP_PERSON_CLASS",
                            "owner_type": "relation",
                            "owner_id": "R_RESIDENCE",
                            "name": "人员类别",
                            "semantics": "Duplicate entity attribute that must not be modeled on relation.",
                            "aliases": [],
                            "local_refs": [
                                {
                                    "question_id": "q1",
                                    "local_name": "居住关系",
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        if '"stage": "topic_clustering"' in prompt:
            return json.dumps(
                {
                    "topics": [
                        {
                            "topic_id": "T_POPULATION_BY_AREA",
                            "name": "区域人口",
                            "question_ids": ["q1", "q2"],
                            "entity_ids": ["E_PERSON", "E_AREA"],
                            "relation_ids": ["R_RESIDENCE"],
                            "attribute_ids": [
                                "A_PERSON_CLASS",
                                "A_PERSON_AGE",
                                "A_REL_RESIDENT_STATUS",
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"unexpected prompt: {prompt[:200]}")

    def batch_single_turn(self, prompts: list[str], **_: object) -> list[str]:
        self.batch_calls.append(prompts)
        outputs: list[str] = []
        for prompt in prompts:
            if '"owner_id": "E_PERSON"' in prompt:
                outputs.append(
                    json.dumps(
                        {
                            "canonical_attributes": [
                                {
                                    "attribute_id": "A_PERSON_AGE",
                                    "owner_type": "entity",
                                    "owner_id": "E_PERSON",
                                    "name": "年龄",
                                    "aliases": ["岁数"],
                                    "question_ids": ["q1", "q2"],
                                }
                            ],
                            "attribute_mappings": [
                                {
                                    "question_id": "q1",
                                    "owner_type": "entity",
                                    "owner_id": "E_PERSON",
                                    "local_name": "年龄",
                                    "attribute_id": "A_PERSON_AGE",
                                },
                                {
                                    "question_id": "q2",
                                    "owner_type": "entity",
                                    "owner_id": "E_PERSON",
                                    "local_name": "岁数",
                                    "attribute_id": "A_PERSON_AGE",
                                },
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
            elif '"owner_id": "R_RESIDENCE"' in prompt:
                outputs.append(
                    json.dumps(
                        {
                            "canonical_attributes": [
                                {
                                    "attribute_id": "A_REL_RESIDENT_STATUS",
                                    "owner_type": "relation",
                                    "owner_id": "R_RESIDENCE",
                                    "name": "常住人口认定状态",
                                    "aliases": ["常住状态"],
                                    "question_ids": ["q1", "q2"],
                                }
                            ],
                            "attribute_mappings": [
                                {
                                    "question_id": "q1",
                                    "owner_type": "relation",
                                    "owner_id": "R_RESIDENCE",
                                    "local_name": "常住人口认定状态",
                                    "attribute_id": "A_REL_RESIDENT_STATUS",
                                },
                                {
                                    "question_id": "q2",
                                    "owner_type": "relation",
                                    "owner_id": "R_RESIDENCE",
                                    "local_name": "常住状态",
                                    "attribute_id": "A_REL_RESIDENT_STATUS",
                                },
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                outputs.append(
                    json.dumps(
                        {"canonical_attributes": [], "attribute_mappings": []},
                        ensure_ascii=False,
                    )
                )
        return outputs


def test_buildtopic_runner_aligns_er_attributes_with_llm_and_clusters_topics() -> None:
    work_dir = Path("tests/_tmp") / f"buildtopic_{uuid.uuid4().hex}"
    metadata_path = work_dir / "metadata"
    output_dir = work_dir / "topic_output"
    log_dir = work_dir / "log"
    write_json(
        metadata_path / "q1" / "nl2er.json",
        {
            "question_id": "q1",
            "user_intent": "大桥街道80岁以上常住人口有多少？",
            "entities": [
                {"name": "人员", "attributes": [{"name": "年龄"}]},
                {"name": "行政区域", "attributes": [{"name": "名称"}]},
            ],
            "relations": [
                {
                    "name": "居住关系",
                    "participants": [
                        {"entity": "人员", "role": "居民"},
                        {"entity": "行政区域", "role": "居住地"},
                    ],
                    "attributes": [{"name": "常住人口认定状态"}],
                }
            ],
        },
    )
    write_json(
        metadata_path / "q2" / "nl2er.json",
        {
            "question_id": "q2",
            "user_intent": "各街道居民岁数分布？",
            "entities": [
                {"entity_name": "居民", "attributes": [{"name": "岁数"}]},
                {"entity_name": "街道", "attributes": [{"name": "名称"}]},
            ],
            "relations": [
                {
                    "relation_name": "居住",
                    "participants": [
                        {"entity": "居民", "role": "居民"},
                        {"entity": "街道", "role": "居住地"},
                    ],
                    "attributes": [{"name": "常住状态"}],
                }
            ],
        },
    )
    llm = RecordingLLM()
    runner = BuildTopicRunner(llm=llm, log_dir=log_dir)

    result = runner.run(metadata_path=metadata_path, output_dir=output_dir)

    assert [call.count('"stage": "entity_alignment"') for call in llm.single_turn_calls][0] == 1
    entity_payload = prompt_payload(llm.single_turn_calls[0])
    assert "questions" not in entity_payload
    assert "local_entities" in entity_payload
    assert all("attributes" not in item for item in entity_payload["local_entities"])
    assert all("question_refs" in item for item in entity_payload["local_entities"])
    assert all("questions" not in item for item in entity_payload["local_entities"])
    assert all("local_refs" not in item for item in entity_payload["local_entities"])
    assert all(
        set(ref.keys()) == {"question_id", "question"}
        for item in entity_payload["local_entities"]
        for ref in item["question_refs"]
    )
    relation_payload = prompt_payload(llm.single_turn_calls[1])
    assert "questions" not in relation_payload
    assert "canonical_entities" in relation_payload
    assert all("local_refs" not in item for item in relation_payload["canonical_entities"])
    person_context = next(
        item for item in relation_payload["canonical_entities"] if item["entity_id"] == "E_PERSON"
    )
    assert "attributes" in person_context
    assert any(attr["attribute_id"] == "A_PERSON_CLASS" for attr in person_context["attributes"])
    assert all("aliases" not in attr for attr in person_context["attributes"])
    assert "local_relations" in relation_payload
    assert all("participants" in item for item in relation_payload["local_relations"])
    assert all("attributes" not in item for item in relation_payload["local_relations"])
    assert all("question_refs" in item for item in relation_payload["local_relations"])
    assert all("questions" not in item for item in relation_payload["local_relations"])
    assert all("local_refs" not in item for item in relation_payload["local_relations"])
    assert all(
        set(ref.keys()) == {"question_id", "question"}
        for item in relation_payload["local_relations"]
        for ref in item["question_refs"]
    )
    assert all(
        participant.get("entity_id")
        for item in relation_payload["local_relations"]
        for participant in item["participants"]
    )
    assert len(llm.batch_calls) == 1
    assert len(llm.batch_calls[0]) == 3
    assert '"existing_attributes"' in llm.batch_calls[0][0]
    assert "A_PERSON_CLASS" in llm.batch_calls[0][0]
    assert "A_REL_DUP_PERSON_CLASS" not in llm.batch_calls[0][2]
    assert result["global_er"]["entities"][0]["entity_id"] == "E_PERSON"
    assert result["global_er"]["relations"][0]["relation_id"] == "R_RESIDENCE"
    assert all("local_refs" not in item for item in result["global_er"]["entities"])
    assert all("local_refs" not in item for item in result["global_er"]["relations"])
    assert all("local_refs" not in item for item in result["global_er"]["attributes"])
    assert {item["attribute_id"] for item in result["global_er"]["attributes"]} == {
        "A_PERSON_CLASS",
        "A_PERSON_AGE",
        "A_REL_RESIDENT_STATUS",
    }
    assert result["topics"] == [
        {
            "topic_id": "T_POPULATION_BY_AREA",
            "name": "区域人口",
            "question_ids": ["q1", "q2"],
            "entity_ids": ["E_PERSON", "E_AREA"],
            "relation_ids": ["R_RESIDENCE"],
            "attribute_ids": [
                "A_PERSON_CLASS",
                "A_PERSON_AGE",
                "A_REL_RESIDENT_STATUS",
            ],
        }
    ]
    assert (output_dir / "global_er.json").exists()
    assert (output_dir / "topics.json").exists()
    assert (output_dir / "buildtopic_summary.json").exists()
    assert (log_dir / "entity_alignment_prompt.md").exists()
    assert (log_dir / "entity_alignment_response.md").exists()
    assert (log_dir / "relation_alignment_prompt.md").exists()
    assert (log_dir / "relation_alignment_response.md").exists()
    assert (log_dir / "topic_clustering_prompt.md").exists()
    assert (log_dir / "topic_clustering_response.md").exists()
    assert (log_dir / "attribute_alignment" / "attribute_alignment_0001_prompt.md").exists()
    assert (log_dir / "attribute_alignment" / "attribute_alignment_0001_response.md").exists()

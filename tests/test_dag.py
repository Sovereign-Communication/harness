"""Unit tests for task decomposition and dependency DAG engine (harness/dag.py)."""
import json
import unittest

from harness.dag import (
    DAGNode,
    TaskDAG,
    build_decomposition_prompt,
    parse_decomposition_response,
)
from harness.errors import HarnessError


class DAGNodeTests(unittest.TestCase):
    def test_node_creation_and_defaults(self):
        node = DAGNode(node_id="n1", instruction="Do something")
        self.assertEqual(node.node_id, "n1")
        self.assertEqual(node.instruction, "Do something")
        self.assertEqual(node.target_files, ())
        self.assertEqual(node.dependencies, ())
        self.assertIsNone(node.local_gate)
        self.assertEqual(node.complexity_tier, 1)

    def test_node_to_from_dict(self):
        data = {
            "node_id": "n1",
            "instruction": "Fix bug",
            "target_files": ["pkg/a.py"],
            "dependencies": ["n0"],
            "local_gate": "pytest tests/test_a.py",
            "complexity_tier": 2,
        }
        node = DAGNode.from_dict(data)
        self.assertEqual(node.node_id, "n1")
        self.assertEqual(node.instruction, "Fix bug")
        self.assertEqual(node.target_files, ("pkg/a.py",))
        self.assertEqual(node.dependencies, ("n0",))
        self.assertEqual(node.local_gate, "pytest tests/test_a.py")
        self.assertEqual(node.complexity_tier, 2)

        out = node.to_dict()
        self.assertEqual(out["node_id"], "n1")
        self.assertEqual(out["target_files"], ["pkg/a.py"])
        self.assertEqual(out["dependencies"], ["n0"])
        self.assertEqual(out["complexity_tier"], 2)

    def test_node_from_dict_validation(self):
        with self.assertRaises(HarnessError):
            DAGNode.from_dict("not a dict")
        with self.assertRaises(HarnessError):
            DAGNode.from_dict({"instruction": "missing id"})
        with self.assertRaises(HarnessError):
            DAGNode.from_dict({"node_id": "n1", "instruction": ""})

        # Invalid tier defaults to 1
        node = DAGNode.from_dict({"node_id": "n1", "instruction": "ok", "complexity_tier": 99})
        self.assertEqual(node.complexity_tier, 1)
        node_bad_str = DAGNode.from_dict({"node_id": "n1", "instruction": "ok", "complexity_tier": "invalid"})
        self.assertEqual(node_bad_str.complexity_tier, 1)


class TaskDAGTests(unittest.TestCase):
    def test_empty_dag(self):
        dag = TaskDAG(nodes={})
        self.assertEqual(dag.topological_batches(), [])
        self.assertEqual(dag.topological_order(), [])
        self.assertEqual(dag.ready_nodes(set()), [])

    def test_linear_chain(self):
        n1 = DAGNode(node_id="n1", instruction="Step 1")
        n2 = DAGNode(node_id="n2", instruction="Step 2", dependencies=("n1",))
        n3 = DAGNode(node_id="n3", instruction="Step 3", dependencies=("n2",))
        dag = TaskDAG(nodes={"n1": n1, "n2": n2, "n3": n3})

        batches = dag.topological_batches()
        self.assertEqual(len(batches), 3)
        self.assertEqual([n.node_id for n in batches[0]], ["n1"])
        self.assertEqual([n.node_id for n in batches[1]], ["n2"])
        self.assertEqual([n.node_id for n in batches[2]], ["n3"])

        order = dag.topological_order()
        self.assertEqual([n.node_id for n in order], ["n1", "n2", "n3"])

    def test_diamond_graph_parallel_batching(self):
        # A -> B, A -> C, B -> D, C -> D
        a = DAGNode(node_id="A", instruction="Root")
        b = DAGNode(node_id="B", instruction="Left", dependencies=("A",))
        c = DAGNode(node_id="C", instruction="Right", dependencies=("A",))
        d = DAGNode(node_id="D", instruction="Join", dependencies=("B", "C"))
        dag = TaskDAG(nodes={"A": a, "B": b, "C": c, "D": d})

        batches = dag.topological_batches()
        self.assertEqual(len(batches), 3)
        self.assertEqual([n.node_id for n in batches[0]], ["A"])
        self.assertEqual([n.node_id for n in batches[1]], ["B", "C"])
        self.assertEqual([n.node_id for n in batches[2]], ["D"])

        # Ready nodes check
        self.assertEqual([n.node_id for n in dag.ready_nodes(set())], ["A"])
        self.assertEqual([n.node_id for n in dag.ready_nodes({"A"})], ["B", "C"])
        self.assertEqual([n.node_id for n in dag.ready_nodes({"A", "B"})], ["C"])
        self.assertEqual([n.node_id for n in dag.ready_nodes({"A", "B", "C"})], ["D"])
        self.assertEqual(dag.ready_nodes({"A", "B", "C", "D"}), [])

    def test_disconnected_parallel_batch(self):
        # 3 completely independent nodes
        x = DAGNode(node_id="X", instruction="x")
        y = DAGNode(node_id="Y", instruction="y")
        z = DAGNode(node_id="Z", instruction="z")
        dag = TaskDAG(nodes={"X": x, "Y": y, "Z": z})

        batches = dag.topological_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual([n.node_id for n in batches[0]], ["X", "Y", "Z"])

    def test_unknown_dependency_raises(self):
        a = DAGNode(node_id="A", instruction="Root", dependencies=("NON_EXISTENT",))
        with self.assertRaises(HarnessError) as ctx:
            TaskDAG(nodes={"A": a})
        self.assertIn("unknown dependency", str(ctx.exception))

    def test_self_dependency_raises(self):
        a = DAGNode(node_id="A", instruction="Root", dependencies=("A",))
        with self.assertRaises(HarnessError) as ctx:
            TaskDAG(nodes={"A": a})
        self.assertIn("cannot depend on itself", str(ctx.exception))

    def test_mismatched_key_raises(self):
        a = DAGNode(node_id="A", instruction="Root")
        with self.assertRaises(HarnessError) as ctx:
            TaskDAG(nodes={"WRONG_KEY": a})
        self.assertIn("does not match node_id", str(ctx.exception))

    def test_cycle_detection_simple(self):
        # A -> B -> A
        a = DAGNode(node_id="A", instruction="a", dependencies=("B",))
        b = DAGNode(node_id="B", instruction="b", dependencies=("A",))
        with self.assertRaises(HarnessError) as ctx:
            TaskDAG(nodes={"A": a, "B": b})
        self.assertIn("cycle detected", str(ctx.exception))

    def test_cycle_detection_triplet(self):
        # A -> B -> C -> A
        a = DAGNode(node_id="A", instruction="a", dependencies=("C",))
        b = DAGNode(node_id="B", instruction="b", dependencies=("A",))
        c = DAGNode(node_id="C", instruction="c", dependencies=("B",))
        with self.assertRaises(HarnessError) as ctx:
            TaskDAG(nodes={"A": a, "B": b, "C": c})
        self.assertIn("cycle detected", str(ctx.exception))

    def test_to_and_from_dict_and_json(self):
        a = DAGNode(node_id="A", instruction="root", target_files=("a.py",))
        b = DAGNode(node_id="B", instruction="leaf", dependencies=("A",))
        dag = TaskDAG(nodes={"A": a, "B": b})

        d = dag.to_dict()
        reconstructed = TaskDAG.from_dict(d)
        self.assertEqual(len(reconstructed.nodes), 2)
        self.assertEqual(reconstructed.nodes["B"].dependencies, ("A",))

        json_str = dag.to_json()
        from_json_dag = TaskDAG.from_json(json_str)
        self.assertEqual(len(from_json_dag.nodes), 2)

    def test_from_dict_errors(self):
        with self.assertRaises(HarnessError):
            TaskDAG.from_dict("not dict")
        with self.assertRaises(HarnessError):
            TaskDAG.from_dict({"nodes": "not a list"})
        with self.assertRaises(HarnessError):
            TaskDAG.from_dict({"nodes": [{"node_id": "A", "instruction": "1"}, {"node_id": "A", "instruction": "dup"}]})
        with self.assertRaises(HarnessError):
            TaskDAG.from_json("invalid json {")


class DecompositionPromptAndParserTests(unittest.TestCase):
    def test_build_decomposition_prompt(self):
        prompt = build_decomposition_prompt(
            goal="Refactor authentication",
            repo_context="Django app with user models",
            candidate_files=["auth/models.py", "auth/views.py"]
        )
        self.assertIn("Refactor authentication", prompt)
        self.assertIn("Django app with user models", prompt)
        self.assertIn("auth/models.py", prompt)
        self.assertIn("auth/views.py", prompt)
        self.assertIn("complexity_tier", prompt)

    def test_parse_decomposition_response_markdown(self):
        llm_response = """
        Here is the planned work graph:
        ```json
        {
          "nodes": [
            {
              "node_id": "t1",
              "instruction": "Add models",
              "target_files": ["models.py"],
              "dependencies": [],
              "complexity_tier": 0
            },
            {
              "node_id": "t2",
              "instruction": "Add views",
              "target_files": ["views.py"],
              "dependencies": ["t1"],
              "local_gate": "python -m unittest",
              "complexity_tier": 1
            }
          ]
        }
        ```
        Let me know if you want to proceed.
        """
        dag = parse_decomposition_response(llm_response)
        self.assertEqual(len(dag.nodes), 2)
        self.assertEqual(dag.nodes["t1"].complexity_tier, 0)
        self.assertEqual(dag.nodes["t2"].dependencies, ("t1",))

    def test_parse_decomposition_response_raw_json(self):
        data = {
            "nodes": [
                {"node_id": "step1", "instruction": "Clean code", "dependencies": []}
            ]
        }
        dag = parse_decomposition_response(json.dumps(data))
        self.assertEqual(len(dag.nodes), 1)
        self.assertEqual(dag.nodes["step1"].instruction, "Clean code")

    def test_parse_decomposition_response_errors(self):
        with self.assertRaises(HarnessError):
            parse_decomposition_response("")
        with self.assertRaises(HarnessError):
            parse_decomposition_response("No json here whatsoever")
        with self.assertRaises(HarnessError):
            parse_decomposition_response("```json\n{malformed json\n```")


if __name__ == "__main__":
    unittest.main()

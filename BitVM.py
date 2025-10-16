# BitVM Proof Builder: Off-chain Prover + Watcher Infrastructure

import os
import json
import time
import shutil
import subprocess
from pathlib import Path
from hashlib import sha256
from typing import Dict, List

import requests

ROLLUP_DB = Path("rollup_block_db")
ROLLUP_DB.mkdir(exist_ok=True)

HISTORY_FILE = Path("ipfs_commit_history.json")

IPFS_CLUSTER_URL = os.getenv("IPFS_CLUSTER_URL", "http://127.0.0.1:9094/pins")
BITCOIND_RPC_URL = os.getenv("BITCOIND_RPC_URL", "http://127.0.0.1:8332")
BITCOIND_RPC_USER = os.getenv("BITCOIND_RPC_USER", "user")
BITCOIND_RPC_PASS = os.getenv("BITCOIND_RPC_PASS", "password")

class BitVMProofBuilder:
    def fetch_from_ipfs(self, ipfs_hash: str) -> Dict:
        try:
            result = subprocess.run(["ipfs", "get", ipfs_hash, "-o", "_ipfs_block.json"], capture_output=True)
            with open("_ipfs_block.json") as f:
                block_data = json.load(f)
            os.remove("_ipfs_block.json")
            expected = ipfs_hash[:16]
            actual = sha256(json.dumps(block_data).encode()).hexdigest()[:16]
            if expected != actual:
                print(f" Hash mismatch: expected {expected}, got {actual}")
            else:
                print(f" IPFS hash matches block content: {actual}")
            self.store_rollup_block(block_data, block_id=actual)
            return block_data
        except Exception as e:
            print(f" IPFS fetch failed: {e}")
            return {}

    def pin_to_ipfs(self, ipfs_hash: str):
        try:
            subprocess.run(["ipfs", "pin", "add", ipfs_hash], check=True)
            print(f" IPFS hash pinned locally: {ipfs_hash}")
            response = requests.post(IPFS_CLUSTER_URL, json={"cid": ipfs_hash})
            if response.status_code == 202:
                print(f"🔗 Cluster pin request accepted for: {ipfs_hash}")
            else:
                print(f"⚠️ Cluster pin request failed: {response.text}")
        except Exception as e:
            print(f" IPFS pinning error: {e}")

    def fetch_utxos(self, address: str) -> List[Dict]:
        try:
            payload = {
                "jsonrpc": "1.0",
                "id": "curltest",
                "method": "listunspent",
                "params": [0, 9999999, [address]]
            }
            response = requests.post(
                BITCOIND_RPC_URL,
                auth=(BITCOIND_RPC_USER, BITCOIND_RPC_PASS),
                headers={"content-type": "application/json"},
                data=json.dumps(payload)
            )
            result = response.json()
            return result.get("result", [])
        except Exception as e:
            print(f" UTXO fetch failed: {e}")
            return []

    def __init__(self, db_path=ROLLUP_DB):
        self.history = self.load_history()
        self.db_path = db_path
        self.auto_update_utxo_state()

    def auto_update_utxo_state(self):
        tracked_addresses = set()
        for block_file in self.list_blocks():
            block = self.load_block(block_file.split("_")[2].split(".")[0])
            for output in block.get("outputs", []):
                addr = output.get("address")
                if addr:
                    tracked_addresses.add(addr)

        print(f" Auto-updating UTXO state for {len(tracked_addresses)} addresses...")
        for addr in tracked_addresses:
            utxos = self.fetch_utxos(addr)
            print(f"💰 {addr}: {len(utxos)} UTXO(s)")

    def load_history(self):
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE) as f:
                return json.load(f)
        return []

    def save_history(self):
        with open(HISTORY_FILE, "w") as f:
            json.dump(self.history, f, indent=2)

    def store_rollup_block(self, block_data: Dict, block_id: str = None):
        if shutil.which("ipfs"):
            tmp_path = Path("_tmp_block.json")
            with open(tmp_path, "w") as f:
                json.dump(block_data, f)
            result = subprocess.run(["ipfs", "add", "-q", str(tmp_path)], capture_output=True)
            ipfs_hash = result.stdout.decode().strip()
            self.pin_to_ipfs(ipfs_hash)
            print(f" IPFS Hash: {ipfs_hash}")
            self.history.append({"ipfs_hash": ipfs_hash, "timestamp": time.time()})
            self.save_history()
            tmp_path.unlink()
        block_id = block_id or sha256(json.dumps(block_data).encode()).hexdigest()[:16]
        path = self.db_path / f"rollup_block_{block_id}.json"
        with open(path, "w") as f:
            json.dump(block_data, f, indent=2)
        print(f" Stored rollup block: {path.name}")
        return block_id

    def list_blocks(self) -> List[str]:
        return sorted([f.name for f in self.db_path.glob("rollup_block_*.json")])

    def load_block(self, block_id: str) -> Dict:
        path = self.db_path / f"rollup_block_{block_id}.json"
        with open(path) as f:
            return json.load(f)

    def watch_for_challenges(self, interval=5):
        print("👁️ Watching for challenge requests...")
        while True:
            for block_file in self.db_path.glob("rollup_block_*.json"):
                with open(block_file) as f:
                    block = json.load(f)
                    if block.get("challenged") and not block.get("proof_generated"):
                        self.process_challenge(block, block_file)
            time.sleep(interval)

    def process_challenge(self, block: Dict, file_path: Path):
        """Process a single challenge and export proof + PSBT"""
        print(f"⚔️ Processing challenge on block {file_path.name}")
        step_data = block.get("step_chain", [])
        verified = all(
            sha256(bytes.fromhex(step_data[i])).hexdigest() == step_data[i + 1]
            for i in range(len(step_data) - 1)
        )
        block["proof_verified"] = verified
        block["proof_generated"] = True
        with open(file_path, "w") as f:
            json.dump(block, f, indent=2)
                print(f" Proof {'valid' if verified else 'invalid'} for {file_path.name}")

        # Export proof to JSON
        proof_out = file_path.with_name(file_path.stem + "_proof.json")
        with open(proof_out, "w") as f:
            json.dump({"proof_steps": block.get("step_chain"), "verified": verified}, f, indent=2)
        print(f"📄 Exported proof to {proof_out.name}")

        # Generate Taproot PSBT (if external generator installed)
        script_tree = {
            "tapleaf_tree": [
                {
                    "name": f"step_{i}",
                    "script": f"OP_SHA256 {step} OP_EQUAL",
                    "tapleaf_version": "c0"
                } for i, step in enumerate(block.get("step_chain", []))
            ]
        }
        psbt_out = file_path.with_name(file_path.stem + "_challenge.psbt")
        tree_file = file_path.with_name(file_path.stem + "_tree.json")
        with open(tree_file, "w") as f:
            json.dump(script_tree, f, indent=2)
        op_returns = block.get("ipfs_hashes") or [block.get("ipfs_hash")]
        op_returns = [h for h in op_returns if h]
        if len(op_returns) > 4:
            print(f" Too many IPFS hashes for OP_RETURN ({len(op_returns)}), truncating to 4.")
            op_returns = op_returns[:4]
        op_return_script = ' '.join(op_returns)[:80]  # truncate if too long
        os.system(f"taproot-psbt-generator --tree {tree_file} --output {psbt_out} --op_return {op_return_script}")
                print(f" Generated PSBT: {psbt_out.name}")

        # Automatically sign PSBT using HWI and broadcast
        try:
            import subprocess
            signed_psbt = file_path.with_name(file_path.stem + "_signed.psbt")
            final_tx = file_path.with_name(file_path.stem + "_final.tx")
            log_file = file_path.with_name(file_path.stem + "_log.json")

            subprocess.run(["hwi", "--device-type", "ledger", "signtx", "--psbt", str(psbt_out), "--out", str(signed_psbt)], check=True)
            subprocess.run(["hwi", "--device-type", "ledger", "finalizetx", "--psbt", str(signed_psbt), "--out", str(final_tx)], check=True)

            with open(final_tx) as f:
                tx_hex = f.read().strip()

            from bitcoin.rpc import RawProxy
            rpc = RawProxy()
            txid = rpc.sendrawtransaction(tx_hex)
            print(f"📡 Broadcasted TXID: {txid}")

            # Validate OP_RETURN in final tx
            sighash = sha256(tx_hex.encode()).hexdigest()
            print(f" Sighash: {sighash}")

            tx_info = rpc.getrawtransaction(txid, True)
            outputs = tx_info.get("vout", [])
            found_opreturn = False
            for vout in outputs:
                script = vout.get("scriptPubKey", {}).get("asm", "")
                if script.startswith("OP_RETURN") and commitment.hex() in script:
                    found_opreturn = True
                    print(" OP_RETURN commitment found in tx output")
                    break
            if not found_opreturn:
                print(" OP_RETURN commitment NOT found in tx outputs")

            # Append OP_RETURN with commitment hash
            ipfs_hashes = block.get("ipfs_hashes") or [block.get("ipfs_hash")]
            ipfs_hashes = [h for h in ipfs_hashes if h]
            ipfs_hash = ipfs_hashes[0] if ipfs_hashes else None
            if ipfs_hash:
                combined_data = ''.join(ipfs_hashes)
                commitment = sha256(combined_data.encode()).digest()
            else:
            commitment = sha256(''.join(block.get("step_chain", [])).encode()).digest()
                        log = {
                "ipfs_hash": ipfs_hash if ipfs_hash else "N/A",
                "txid": txid,
                "commitment": commitment.hex(),
                "timestamp": time.time()
            }
            with open(log_file, "w") as f:
                json.dump(log, f, indent=2)
                        log["sighash"] = sighash
            print(f" Proof log saved: {log_file.name}")
        except Exception as e:
            print(f" Signing/Broadcasting failed: {e}")

from rich.console import Console
from rich.table import Table

from rich.tree import Tree
from rich.panel import Panel
from rich import print as rprint

if __name__ == "__main__":
    # Optional fetch from IPFS example:
    # ipfs_block = prover.fetch_from_ipfs("<ipfs_hash_here>")
    # prover.store_rollup_block(ipfs_block)

    prover = BitVMProofBuilder()
    # prover.watch_for_challenges()  # Uncomment for daemon mode

    # Visualize IPFS commit history
    if prover.history:
        console = Console()
        table = Table(title="IPFS Commit History")
        table.add_column("#", justify="right")
        table.add_column("IPFS Hash", style="cyan")
        table.add_column("Timestamp", style="green")
        for i, entry in enumerate(prover.history):
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['timestamp']))
            table.add_row(str(i + 1), entry['ipfs_hash'], ts)
        console.print(table)

        # Filter by date (example: last 24 hours)
        hours = 24
        now = time.time()
        recent = [entry for entry in prover.history if (now - entry['timestamp']) <= hours * 3600]
        if recent:
            table_recent = Table(title=f"Recent Commits (< {hours}h)")
            table_recent.add_column("#", justify="right")
            table_recent.add_column("IPFS Hash", style="cyan")
            table_recent.add_column("Timestamp", style="green")
            for i, entry in enumerate(recent):
                ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['timestamp']))
                table_recent.add_row(str(i + 1), entry['ipfs_hash'], ts)
            console.print(table_recent)

        # Graph structure visualization
        tree = Tree(" [bold green]IPFS Commit Graph[/bold green]")
        for i, entry in enumerate(prover.history[-10:]):
            node = tree.add(f"{i+1}. {entry['ipfs_hash'][:16]} @ {time.strftime('%m-%d %H:%M', time.localtime(entry['timestamp']))}")
            if i > 0:
                node.add(f"linked from → {prover.history[i-1]['ipfs_hash'][:16]}")
        rprint(Panel(tree, title="Latest Commit Chain"))

        # Export DOT and Mermaid.js
        dot_path = Path("ipfs_graph.dot")
        mermaid_path = Path("ipfs_graph.mmd")
        with open(dot_path, "w") as f:
            f.write("digraph IPFSGraph {
")
            for i, entry in enumerate(prover.history[-10:]):
                node = f"\"{entry['ipfs_hash'][:16]}\""
                f.write(f"  {node};
")
                if i > 0:
                    prev = f"\"{prover.history[i-1]['ipfs_hash'][:16]}\""
                    f.write(f"  {prev} -> {node};
")
            f.write("}
")

        with open(mermaid_path, "w") as f:
            f.write("graph TD
")
            for i, entry in enumerate(prover.history[-10:]):
                node = entry['ipfs_hash'][:16]
                f.write(f"  {node}
")
                if i > 0:
                    prev = prover.history[i-1]['ipfs_hash'][:16]
                    f.write(f"  {prev} --> {node}
")
        print(f" Exported IPFS graph to {dot_path.name} and {mermaid_path.name}")

    # Example: add a block manually
    demo_block = {
        "timestamp": time.time(),
        "step_chain": [
            sha256(b"rollup_state").hexdigest(),
            sha256(sha256(b"rollup_state").digest()).hexdigest()
        ],
        "challenged": True
    }
    prover.store_rollup_block(demo_block)
    prover.watch_for_challenges()

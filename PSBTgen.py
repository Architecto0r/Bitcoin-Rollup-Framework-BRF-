from bitcointx import select_chain_params
from bitcointx.core import COutPoint, CTxIn, CTxOut, CTransaction, lx, b2x, CScript
from bitcointx.core.script import OP_CHECKSIGVERIFY, OP_CHECKSEQUENCEVERIFY, OP_EQUAL, OP_RETURN, OP_HASH256, OP_IF, OP_ELSE, OP_ENDIF, OP_SHA256
from bitcointx.core.key import CBitcoinSecret, CPubKey
from bitcointx.wallet import P2TRBitcoinAddress
from bitcointx.taproot import TaprootScriptTree, TaprootLeaf, constructTaprootOutputKey, TaprootSignatureHash, TapLeafInfo
from bitcointx.core.psbt import PartiallySignedTransaction as PSBT
from bitcointx.core.script import SIGHASH_ALL
from hashlib import sha256

# Simulated BitVM SHA256 step chain
class BitVM:
    @staticmethod
    def step_chain(seed: bytes, steps: int) -> list:
        chain = [seed]
        for _ in range(steps):
            chain.append(sha256(chain[-1]).digest())
        return chain[::-1]

# Set network
select_chain_params("regtest")

# === Step 1: Define keys ===
operator_priv = CBitcoinSecret('cVxkBsK9JeM8e58WqNR52rMHeZVsz5RzsmkAgcYbhXMPZDLZAXL4')
operator_pub = operator_priv.pub
challenger_priv = CBitcoinSecret('cNfwtBa5UGrUBC1PMadq9n56Km2rKm6i9LNj1ZFb1VG3AxErM6P1')
challenger_pub = challenger_priv.pub

# === Step 2: Build auto-transition SHA256 chain scripts ===
chain = BitVM.step_chain(b'init', 3)
timeouts = [80, 160, 240]
scripts = []

for i in range(len(chain) - 1):
    current_data = chain[i+1]
    expected_hash = chain[i]
    timeout = timeouts[i]
    next_data = chain[i] if i < len(chain) - 2 else b'final'
    script = CScript([
        challenger_pub, OP_CHECKSIGVERIFY,
        current_data, OP_SHA256, expected_hash, OP_EQUAL, OP_IF,
            OP_1,
        OP_ELSE,
            timeout, OP_CHECKSEQUENCEVERIFY,
            operator_pub, OP_CHECKSIGVERIFY,
        OP_ENDIF
    ])
    scripts.append(TaprootLeaf(script))

# === Operator fallback ===
leaf_op = TaprootLeaf(CScript([operator_pub, OP_CHECKSIGVERIFY, 300, OP_CHECKSEQUENCEVERIFY]))

# === Build Taproot address ===
tree = TaprootScriptTree([leaf_op] + scripts)
taproot_key = constructTaprootOutputKey(operator_pub, tree)
taproot_addr = P2TRBitcoinAddress.from_output_key(taproot_key)
print("\n Taproot address:", taproot_addr)

# === Build PSBTs for each challenge step with OP_RETURN logging ===
from bitcointx.core import x
psbts = []

for i, leaf in enumerate(scripts):
    current_data = chain[i+1]
    expected_hash = chain[i]
    script = leaf.script

    # UTXO being spent
    prev_txid = lx("9d5d817f9f8f6952962d967edfc95e9cf0c72f4cb91a6caca03e4efc70cd4342")
    vout = 1
    amount_sats = 21000000000
    fee = 1000

    txin = CTxIn(COutPoint(prev_txid, vout), nSequence=timeouts[i])
    import time
    user_id = b'user42'
    step_hash = sha256(current_data).digest()[:4]  # short hash for brevity
    timestamp = int(time.time()).to_bytes(4, 'big')
    log_data = b'Step' + bytes([i+1]) + b'|' + user_id + b'|' + step_hash + b'|' + timestamp
    op_return_script = CScript([OP_RETURN, log_data])
    txout_main = CTxOut(amount_sats - fee, taproot_addr.to_scriptPubKey())
    txout_log = CTxOut(0, op_return_script)
    tx = CTransaction([txin], [txout_main, txout_log])
    tx = CTransaction([txin], [txout])
    psbt = PSBT.from_transaction(tx)

    # Подпись
    sighash = TaprootSignatureHash(
        tx=tx,
        spent_utxos=[(amount_sats, taproot_addr.to_scriptPubKey())],
        input_index=0,
        scriptpath=True,
        tapleaf_script=script,
        leaf_ver=0xc0,
        sighash_type=SIGHASH_ALL
    )
    sig = challenger_priv.sign_schnorr(sighash) + bytes([SIGHASH_ALL])
    psbt.inputs[0].tap_script_sigs = {(challenger_pub, script): sig}

    control_block = next(li.control_block for li in tree.get_tapleaf_infos() if li.script == script)
    psbt.inputs[0].tap_leaf_script = [{
        "script": script,
        "control": control_block,
        "leaf_version": 0xc0
    }]
    psbt.inputs[0].tap_script_witness = [current_data]
    psbts.append(psbt)

# === Output PSBTs (base64 and hex + save to .psbt files) ===
for i, psbt in enumerate(psbts):
    print(f"\n PSBT for challenge step {i+1} (base64):\n{psbt.to_base64()}")
    print(f"PSBT for challenge step {i+1} (hex):{b2x(psbt.serialize())}")
        # Save to .psbt (binary)
    with open(f"step{i+1}.psbt", "wb") as f:
        f.write(psbt.serialize())

    # Save to .base64
    with open(f"step{i+1}.base64", "w") as f:
        f.write(psbt.to_base64())

    # Save to .json
    import json
    psbt_json = {
        "step": i + 1,
        "base64": psbt.to_base64(),
        "hex": b2x(psbt.serialize()),
        "input_data": current_data.hex(),
        "expected_hash": expected_hash.hex(),
        "control_block": control_block.hex(),
        "signature": sig.hex(),
        "witness": [current_data.hex()],
        "transaction_hex": b2x(tx.serialize()),
        "sighash": sighash.hex(),
        "tapleaf_version": "c0",
        "script_pubkey": taproot_addr.to_scriptPubKey().hex()
    }
    with open(f"step{i+1}.json", "w") as f:
        json.dump(psbt_json, f, indent=2)

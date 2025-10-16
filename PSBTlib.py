# psbtlib.py — Utility to generate, sign, and broadcast punishment PSBT
from bitcointx.wallet import PSBT, PSBTInput, PSBTOutput
from bitcointx.core import COutPoint, CTxOut, CScript, lx
from bitcointx.core.key import CPubKey
from bitcointx.core.script import OP_CHECKSIG
from bitcointx import select_chain_params
import base64
import json
import os
import subprocess
from bitcoin.rpc import RawProxy

select_chain_params('testnet')
BITCOIN_RPC = RawProxy()

def create_punishment_psbt(txid, vout, amount, save_path="."):
    """
    Create, sign, verify, and broadcast a punishment PSBT.
    Outputs .psbt and .json files.
    """
    pubkey = CPubKey(bytes.fromhex("03" + "00" * 32))
    output_script = CScript([pubkey, OP_CHECKSIG])
    tx_out = CTxOut(int(amount * 1e8), output_script)
    outpoint = COutPoint(lx(txid), vout)
    psbt_in = PSBTInput(witness_utxo=tx_out)

    outputs = [
        PSBTOutput(amount=int(amount * 0.9 * 1e8), script_pubkey=output_script),
        PSBTOutput(amount=int(amount * 0.1 * 1e8), script_pubkey=output_script)
    ]

    psbt = PSBT(inputs=[psbt_in], outputs=outputs)
    psbt.inputs[0].utxo = tx_out
    psbt.tx.vin[0].prevout = outpoint

    psbt_b64 = psbt.to_base64()
    filename = f"punishment_{txid}"

    # Save .psbt
    with open(os.path.join(save_path, filename + ".psbt"), "w") as f:
        f.write(psbt_b64)

    # Save .json
    with open(os.path.join(save_path, filename + ".json"), "w") as f:
        json.dump({"psbt": psbt_b64, "txid": txid, "vout": vout, "amount": amount}, f, indent=2)

    try:
        signed = subprocess.check_output([
            "hwi", "--device-path", "/dev/hidraw0", "signtx", "--psbt", psbt_b64
        ]).decode()

        signed_filename = os.path.join(save_path, filename + "_signed.psbt")
        with open(signed_filename, "w") as f:
            f.write(signed)

        # Extract final tx and broadcast
        final_tx = subprocess.check_output([
            "hwi", "--device-path", "/dev/hidraw0", "finalizepsbt", "--psbt", signed
        ]).decode()

        tx_hex = json.loads(final_tx).get("hex")
        if tx_hex:
            txid = BITCOIN_RPC.sendrawtransaction(tx_hex)
            print(f" Broadcasted TXID: {txid}")
        else:
            print(" Could not finalize PSBT to hex")

    except Exception as e:
        print(f" HWI signing or broadcasting failed: {e}")

            # Verify UTXO broadcast
        try:
            tx_details = BITCOIN_RPC.getrawtransaction(txid, True)
            if tx_details:
                print(f" Verified on-chain UTXO: {txid} with outputs:")
                for i, vout in enumerate(tx_details['vout']):
                    print(f"  → Output {i}: {vout['value']} BTC to {vout['scriptPubKey']['address'] if 'address' in vout['scriptPubKey'] else 'script'}")
        except Exception as e:
            print(f" Could not verify UTXO on-chain: {e}")

        # Log results
    try:
        log_path = os.path.join(save_path, "punishment_log.json")
        log_entry = {
            "txid": txid,
            "timestamp": int(time.time()),
            "outputs": tx_details.get("vout", [])
        }
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                logs = json.load(f)
        else:
            logs = []
        logs.append(log_entry)
        with open(log_path, "w") as f:
            json.dump(logs, f, indent=2)
        print(f" Log entry added to {log_path}")
    except Exception as e:
        print(f" Could not write log: {e}")

    return psbt_b64

#!/bin/bash
# Round-4 re-audit: merged harness (gemma judge, deterministic tally, hard participation)
set -u
cd "C:/Users/SCM/Documents/GitHub/Harness"
declare -A P=(
  [01]=audits/scmessenger/prompts/01b_negotiate_suite_consensus.txt
  [02]=audits/scmessenger/prompts/02b_decrypt_ratcheted_v2_consensus.txt
  [03]=audits/scmessenger/prompts/03b_ratchet_encrypt_consensus.txt
  [04]=audits/scmessenger/prompts/04b_ratchet_decrypt_consensus.txt
  [05]=audits/scmessenger/prompts/05b_decode_wire_signed_envelope_consensus.txt
  [06]=audits/scmessenger/prompts/06c_construct_onion_consensus.txt
  [07]=audits/scmessenger/prompts/07c_peel_layer_consensus.txt
  [08]=audits/scmessenger/prompts/08c_safety_number_consensus.txt
  [09]=audits/scmessenger/prompts/09b_verify_bundle_consensus.txt
)
for n in 01 02 03 04 05 06 07 08 09; do
  out="audits/scmessenger/_runs/v4/${n}_v3.json"
  echo "=== $n ==="
  python -c "
import sys; sys.argv=['harness','verify','--prompt-file','${P[$n]}','--converge','--max-cost','0.02','--task-id','v4-${n}','--out','$out']
from harness.cli import main
main()" 2>"audits/scmessenger/_runs/v4/${n}_v3.log"
  echo "exit=$?"
done
echo DONE

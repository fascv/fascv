#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs

wait_http() {
  local url="$1"
  local deadline_s="${2:-20}"
  local start_ts
  start_ts="$(date +%s)"
  while true; do
    if curl -fsS -m 0.5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    if (( "$(date +%s)" - start_ts >= deadline_s )); then
      return 1
    fi
    sleep 1
  done
}

start_lane() {
  local name="$1"
  local config="$2"
  local control_port="$3"
  local exec_port="$4"
  local journal_port="$5"
  local core_port="$6"
  local md_port="$7"

  local pid_file="logs/${name}_rotation_guard.pid"
  local child_pid_file="logs/${name}_rotation_guard.child.pid"
  local disable_file="logs/${name}_rotation_guard.disabled"
  local guard_log="logs/${name}_rotation_guard.log"

  rm -f "$disable_file"

  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "${name}: already running (guard pid=$pid)"
      return 0
    fi
    rm -f "$pid_file"
  fi

  if command -v setsid >/dev/null 2>&1; then
    setsid env \
      MODE=live \
      CONFIG="$config" \
      START_IMPACT_CONSOLE=0 \
      START_JOURNAL_GUI=0 \
      START_CORE_GUI=0 \
      START_MD_GUI=0 \
      START_EXEC=0 \
      CONTROL_PORT="$control_port" \
      EXEC_PORT="$exec_port" \
      JOURNAL_GUI_PORT="$journal_port" \
      CORE_GUI_PORT="$core_port" \
      MD_GUI_PORT="$md_port" \
      GUARD_LOG="$guard_log" \
      PID_FILE="$pid_file" \
      CHILD_PID_FILE="$child_pid_file" \
      DISABLE_FILE="$disable_file" \
      ./scripts/live_guard.sh >> "$guard_log" 2>&1 < /dev/null &
  else
    nohup env \
      MODE=live \
      CONFIG="$config" \
      START_IMPACT_CONSOLE=0 \
      START_JOURNAL_GUI=0 \
      START_CORE_GUI=0 \
      START_MD_GUI=0 \
      START_EXEC=0 \
      CONTROL_PORT="$control_port" \
      EXEC_PORT="$exec_port" \
      JOURNAL_GUI_PORT="$journal_port" \
      CORE_GUI_PORT="$core_port" \
      MD_GUI_PORT="$md_port" \
      GUARD_LOG="$guard_log" \
      PID_FILE="$pid_file" \
      CHILD_PID_FILE="$child_pid_file" \
      DISABLE_FILE="$disable_file" \
      ./scripts/live_guard.sh >> "$guard_log" 2>&1 < /dev/null &
  fi

  sleep 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    echo "${name}: start requested; guard pid not yet written (check $guard_log)" >&2
    return 1
  fi

  if wait_http "http://127.0.0.1:${control_port}/health" 25; then
    echo "${name}: started (guard pid=$pid, control=http://127.0.0.1:${control_port}/)"
  else
    echo "${name}: guard running (pid=$pid), but control did not become ready yet (check $guard_log)" >&2
    return 1
  fi
}

start_lane "zro" "configs/live_binance_zro_usdc_rotation.yaml" 8206 13310 13320 13330 13340
start_lane "btc" "configs/live_binance_btc_usdc_rotation.yaml" 8208 13410 13420 13430 13440
start_lane "bnb" "configs/live_binance_bnb_usdc_rotation.yaml" 8068 11410 11420 11430 11440
start_lane "bch" "configs/live_binance_bch_usdc_rotation.yaml" 8072 11610 11620 11630 11640
start_lane "eth" "configs/live_binance_eth_usdc_rotation.yaml" 8064 11210 11220 11230 11240
start_lane "sol" "configs/live_binance_sol_usdc_rotation.yaml" 8066 11310 11320 11330 11340
start_lane "chz" "configs/live_binance_chz_usdc_rotation.yaml" 8210 13510 13520 13530 13540
start_lane "ltc" "configs/live_binance_ltc_usdc_rotation.yaml" 8070 11510 11520 11530 11540
start_lane "trx" "configs/live_binance_trx_usdc_rotation.yaml" 8044 10210 10220 10230 10240
start_lane "xrp" "configs/live_binance_xrp_usdc_rotation.yaml" 8024 9210 9220 9230 9240
start_lane "link" "configs/live_binance_link_usdc_rotation.yaml" 8030 9510 9520 9530 9540
start_lane "cake" "configs/live_binance_cake_usdc_rotation.yaml" 8212 13610 13620 13630 13640
start_lane "tao" "configs/live_binance_tao_usdc_rotation.yaml" 8214 13710 13720 13730 13740
start_lane "zen" "configs/live_binance_zen_usdc_rotation.yaml" 8216 13810 13820 13830 13840
start_lane "xtz" "configs/live_binance_xtz_usdc_rotation.yaml" 8102 13110 13120 13130 13140
start_lane "qnt" "configs/live_binance_qnt_usdc_rotation.yaml" 8218 13910 13920 13930 13940
start_lane "xlm" "configs/live_binance_xlm_usdc_rotation.yaml" 8054 10710 10720 10730 10740
start_lane "doge" "configs/live_binance_doge_usdc_rotation.yaml" 8028 9410 9420 9430 9440
start_lane "aave" "configs/live_binance_aave_usdc_rotation.yaml" 8048 10410 10420 10430 10440
start_lane "algo" "configs/live_binance_algo_usdc_rotation.yaml" 8052 10610 10620 10630 10640
start_lane "ethfi" "configs/live_binance_ethfi_usdc_rotation.yaml" 8220 14010 14020 14030 14040
start_lane "uni" "configs/live_binance_uni_usdc_rotation.yaml" 8046 10310 10320 10330 10340
start_lane "trb" "configs/live_binance_trb_usdc_rotation.yaml" 8222 14110 14120 14130 14140
start_lane "near" "configs/live_binance_near_usdc_rotation.yaml" 8008 8410 8420 8430 8440
start_lane "pundix" "configs/live_binance_pundix_usdc_rotation.yaml" 8224 14210 14220 14230 14240
start_lane "hbar" "configs/live_binance_hbar_usdc_rotation.yaml" 8016 8810 8820 8830 8840
start_lane "neo" "configs/live_binance_neo_usdc_rotation.yaml" 8104 13210 13220 13230 13240
start_lane "shib" "configs/live_binance_shib_usdc_rotation.yaml" 8094 12710 12720 12730 12740
start_lane "atom" "configs/live_binance_atom_usdc_rotation.yaml" 8038 9910 9920 9930 9940
start_lane "crv" "configs/live_binance_crv_usdc_rotation.yaml" 8088 12410 12420 12430 12440
start_lane "ton" "configs/live_binance_ton_usdc_rotation.yaml" 8058 10910 10920 10930 10940
start_lane "pol" "configs/live_binance_pol_usdc_rotation.yaml" 8226 14310 14320 14330 14340
start_lane "icp" "configs/live_binance_icp_usdc_rotation.yaml" 8078 11910 11920 11930 11940
start_lane "avax" "configs/live_binance_avax_usdc_rotation.yaml" 8032 9610 9620 9630 9640
start_lane "gmx" "configs/live_binance_gmx_usdc_rotation.yaml" 8228 14410 14420 14430 14440
start_lane "render" "configs/live_binance_render_usdc_rotation.yaml" 8012 8610 8620 8630 8640
start_lane "virtual" "configs/live_binance_virtual_usdc_rotation.yaml" 8230 14510 14520 14530 14540
start_lane "ada" "configs/live_binance_ada_usdc_rotation.yaml" 8026 9310 9320 9330 9340
start_lane "steem" "configs/live_binance_steem_usdc_rotation.yaml" 8232 14610 14620 14630 14640
start_lane "t" "configs/live_binance_t_usdc_rotation.yaml" 8234 14710 14720 14730 14740
start_lane "pepe" "configs/live_binance_pepe_usdc_rotation.yaml" 8236 14810 14820 14830 14840
start_lane "cfx" "configs/live_binance_cfx_usdc_rotation.yaml" 8238 14910 14920 14930 14940
start_lane "ldo" "configs/live_binance_ldo_usdc_rotation.yaml" 8096 12810 12820 12830 12840
start_lane "kaia" "configs/live_binance_kaia_usdc_rotation.yaml" 8240 15010 15020 15030 15040
start_lane "api3" "configs/live_binance_api3_usdc_rotation.yaml" 8242 15110 15120 15130 15140
start_lane "sui" "configs/live_binance_sui_usdc_rotation.yaml" 8062 11110 11120 11130 11140
start_lane "pendle" "configs/live_binance_pendle_usdc_rotation.yaml" 8244 15210 15220 15230 15240
start_lane "bonk" "configs/live_binance_bonk_usdc_rotation.yaml" 8246 15310 15320 15330 15340
start_lane "wld" "configs/live_binance_wld_usdc_rotation.yaml" 8248 15410 15420 15430 15440
start_lane "strk" "configs/live_binance_strk_usdc_rotation.yaml" 8250 15510 15520 15530 15540
start_lane "paxg" "configs/live_binance_paxg_usdc_rotation.yaml" 8252 15610 15620 15630 15640
start_lane "sei" "configs/live_binance_sei_usdc_rotation.yaml" 8040 10010 10020 10030 10040

#!/usr/bin/env bash
# Phase 0 — reusable Cadence/Spectre environment for LDO Target B bring-up.
# Mirrors the live Virtuoso IC618 process env on this box (verified 2026-06-07),
# so the same setup drives spectre CLI (Phase 1/2) AND skillbridge/ADE (Phase 3).
#   usage:  source cadence/env.sh
export CDSHOME=/home/yusheng/Program/eda/cadence/IC618
export CDS_INST_DIR=$CDSHOME
export CDS_ROOT=$CDSHOME
export CDS_LIC_FILE=/home/yusheng/Program/eda/cadence/license/license.dat
export SPECTRE_HOME=/home/yusheng/Program/eda/cadence/SPECTRE181
# Spectre 18.1 FIRST so `spectre`/`spectreVerilog`/VA-compiler resolve to 18.1,
# then the IC618 tools (skillbridge, OCEAN, ddCreateLib live here).
export PATH=$SPECTRE_HOME/bin:$SPECTRE_HOME/tools/bin:$CDSHOME/bin:$CDSHOME/tools/bin/64bit:$CDSHOME/tools/bin:$CDSHOME/share/oa/bin:$CDSHOME/tools/dfII/bin:$PATH

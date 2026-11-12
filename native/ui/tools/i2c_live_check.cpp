/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Manual, interactive qualification. Opening is read-only; every write requires
// an explicit command on stdin. Uses exactly the native UI worker backend.
#include "backend.h"
#include <iostream>
#include <sstream>
#include <string>
using namespace druta::ui;
int wmain(int argc,wchar_t** argv) {
    if (argc!=3) { std::cerr<<"usage: i2c_live_check ABSOLUTE_DLL ASCII_GPU_SLOT\n"; return 1; }
    std::wstring wide_slot=argv[2]; std::string slot;
    for (wchar_t ch:wide_slot) { if(ch>127) return 1; slot.push_back(static_cast<char>(ch)); }
    auto backend=create_core_backend(argv[1]); std::string error;
    if (!backend->open(slot,error)) { std::cerr<<error<<'\n'; return 2; }
    std::cout<<"Ready. Commands: select PORT ADDR_DECIMAL, scan, gate 0|1, verify, apply MV, read, release, quit\n"<<std::flush;
    std::string line;
    while (std::getline(std::cin,line)) {
        std::istringstream input(line); std::string op; input>>op; Command c;
        if (op=="quit") {
            auto r=backend->prepare_close(); std::cout<<"close ok="<<r.ok<<" "<<r.message<<'\n'<<std::flush;
            if (r.ok) {backend->close();return 0;} continue;
        }
        if (op=="select") { int port=-1,addr=-1; input>>port>>addr; if(port<0 || port>7 || addr<8 || addr>0x77) continue;
            c.operation=Command::Operation::i2c_select; c.i2c_route=I2cRoute{static_cast<uint8_t>(port),static_cast<uint8_t>(addr)};
        } else if(op=="scan") c.operation=Command::Operation::i2c_scan;
        else if(op=="read") c.operation=Command::Operation::i2c_read;
        else if(op=="gate") {c.operation=Command::Operation::i2c_gate;int gate=0;input>>gate;c.enabled=gate!=0;}
        else if(op=="verify") c.operation=Command::Operation::i2c_verify;
        else if(op=="apply") {c.operation=Command::Operation::i2c_apply;if(!(input>>c.value))continue;}
        else if(op=="release") c.operation=Command::Operation::i2c_release;
        else continue;
        auto r=backend->execute(c); auto live=backend->read(false); const auto& d=live.i2c;
        std::cout<<"operation="<<op<<" ok="<<r.ok<<" changed="<<r.may_have_changed<<" message="<<r.message<<'\n';
        std::cout<<"gate="<<d.gate<<" verified="<<d.verified<<" owned="<<d.status.owned<<" recovery="<<d.status.recovery_pending
                 <<" clamp="<<d.clock_owned<<" clamp_mhz="<<d.clock_mhz<<" core_mhz="<<live.telemetry.core;
        if(d.status.controller_vout_mv)std::cout<<" controller_mv="<<*d.status.controller_vout_mv;
        if(d.status.requested_mv)std::cout<<" command_mv="<<*d.status.requested_mv;
        if(d.status.min_mv)std::cout<<" minimum_mv="<<*d.status.min_mv;
        if(d.status.max_mv)std::cout<<" maximum_mv="<<*d.status.max_mv;
        std::cout<<" capable="<<d.status.capable<<" writable="<<d.status.writable<<" status="<<d.status.message<<'\n'<<std::flush;
    }
    const auto released=backend->prepare_close(); std::cerr<<"EOF cleanup: "<<released.message<<'\n';
    if (released.ok) {backend->close();return 0;}
    std::cerr<<"Recovery incomplete; controller/clock request may remain active\n";return 3;
}

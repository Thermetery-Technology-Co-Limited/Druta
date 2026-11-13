// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include "policy_annotation_profile.h"
#include "python_full_profile.h"
#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <memory>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
static bool until(const std::function<bool()>& predicate) {
    const auto deadline=GetTickCount64()+3000;
    while(GetTickCount64()<deadline) { if(predicate())return true; Sleep(1); }
    return predicate();
}
struct AnnotationBackend final : IBackend {
    std::atomic<unsigned> actions{0}, reads{0};
    std::atomic<bool> fail_action{false};
    std::atomic<bool> disconnected{false}, metadata_undo{false};
    std::atomic<unsigned> identity_mode{0};
    std::string slot;
    std::shared_ptr<const PolicyAnnotationEffect> effect=std::make_shared<const PolicyAnnotationEffect>();
    std::vector<Device> enumerate() override { return {{"a","A","1"},{"b","B","2"}}; }
    bool open(const std::string& selected,std::string&) override { slot=selected;return true; }
    void close() override {}
    LiveData read(bool) override {
        ++reads; LiveData data;data.connected=!disconnected.load();data.status="ok";
        data.undo_metadata_only=metadata_undo.load();data.identity.nvapi_ok=1;
        const auto mode=identity_mode.load();
        std::snprintf(data.identity.slot,sizeof(data.identity.slot),"%s",mode==2?"wrong":slot.c_str());
        std::snprintf(data.identity.uuid,sizeof(data.identity.uuid),"%s",mode==1?"":mode==3?"wrong":slot=="a"?"1":"2");
        if(mode==4)std::snprintf(data.identity.pairing_error,sizeof(data.identity.pairing_error),"unpaired");
        if(mode==5)data.identity.nvapi_ok=0;
        return data;
    }
    ActionResult execute(const Command& command) override {
        ++actions; ActionResult result; result.ok=!fail_action.load(); result.message=result.ok?"complete":"hardware failed";
        if(command.operation==Command::Operation::apply_catalog_profile || command.operation==Command::Operation::undo)
            result.annotation_effect=effect;
        return result;
    }
};
static void disconnected_metadata(const std::string& expected_uuid) {
    using Op=Command::Operation;
    auto backend=std::make_unique<AnnotationBackend>();auto* fake=backend.get();
    Session session(std::move(backend),[]{});session.start("a",expected_uuid);
    CHECK(until([&]{auto s=session.snapshot();return !s->opening&&s->data.connected;}));
    const auto generation=session.snapshot()->generation;
    ProfileJson full={{"schema",2},{"device",{{"uuid","1"},{"slot","a"}}},{"incomplete",ProfileJson::array()},
        {"mem_off_units",80},{"mem_off_true_mhz",40},{"mem_off_scale",2},
        {"power_policy_names",{{"3",{{"name","PCIe"},{"channel",7},{"type",4}}}}}};
    const auto notes=select_python_full_profile(full,{"annotation:power"});
    Command command;command.catalog_profile=std::make_shared<const ProfileJson>(notes);
    const auto refresh=[&] {
        const auto before=session.snapshot()->completed_reads;session.request_poll(true);
        CHECK(until([&]{const auto s=session.snapshot();const auto mode=fake->identity_mode.load();
            return s->completed_reads>before && !s->data.connected &&
                s->data.undo_metadata_only==fake->metadata_undo.load() &&
                std::string(s->data.identity.uuid)==(mode==1?"":mode==3?"wrong":"1") &&
                std::string(s->data.identity.slot)==(mode==2?"wrong":"a") &&
                std::string(s->data.identity.pairing_error)==(mode==4?"unpaired":"") &&
                s->data.identity.nvapi_ok==(mode==5?0u:1u);
        }));
    };
    const auto accepted=[&](Op op) {
        command.operation=op;const auto before=session.snapshot()->completed_actions;
        CHECK(session.submit(command,generation));
        CHECK(until([&]{auto s=session.snapshot();return !s->busy&&s->completed_actions>before;}));
    };
    fake->disconnected=true;fake->metadata_undo=true;refresh();
    CHECK(!session.snapshot()->data.connected);
    accepted(Op::preflight_catalog_profile);accepted(Op::apply_catalog_profile);accepted(Op::undo);
    const auto before=fake->actions.load();
    command.operation=Op::apply_catalog_profile;command.catalog_profile=std::make_shared<const ProfileJson>(full);
    CHECK(!session.submit(command,generation));
    command.operation=Op::preflight_catalog_profile;CHECK(!session.submit(command,generation));
    command.catalog_profile=std::make_shared<const ProfileJson>(select_python_full_profile(full,{"clock:memory"}));
    CHECK(!session.submit(command,generation));
    command.catalog_profile=std::make_shared<const ProfileJson>(ProfileJson{{"scope","unknown"}});
    CHECK(!session.submit(command,generation));
    command.catalog_profile=std::make_shared<const ProfileJson>(notes);
    CHECK(!session.submit(command,generation+1));
    fake->metadata_undo=false;refresh();command.operation=Op::undo;CHECK(!session.submit(command,generation));
    fake->metadata_undo=true;
    for(unsigned mode=1;mode<=5;++mode) {
        if(mode==3&&expected_uuid.empty())continue; // no startup UUID constraint in this variant
        fake->identity_mode=mode;refresh();command.operation=Op::apply_catalog_profile;
        CHECK(!session.submit(command,generation));command.operation=Op::preflight_catalog_profile;
        CHECK(!session.submit(command,generation));command.operation=Op::undo;CHECK(!session.submit(command,generation));
    }
    CHECK(fake->actions.load()==before);
    fake->identity_mode=0;refresh();accepted(Op::apply_catalog_profile);
    session.begin_shutdown();CHECK(until([&]{return session.finished();}));
}
int main() {
    disconnected_metadata("1");disconnected_metadata({});
    using Op=Command::Operation;
    auto backend=std::make_unique<AnnotationBackend>(); auto* fake=backend.get();
    Session session(std::move(backend),[]{}); session.start("a");
    CHECK(until([&]{auto s=session.snapshot();return !s->opening&&s->data.connected;}));
    auto generation=session.snapshot()->generation;
    const auto execute=[&](Op op) {
        const auto before=session.snapshot()->completed_actions;
        Command command;command.operation=op; CHECK(session.submit(command,generation));
        CHECK(until([&]{auto s=session.snapshot();return !s->busy&&s->completed_actions>before;}));
    };
    execute(Op::apply_catalog_profile);
    const auto original=session.snapshot(); CHECK(original->annotation_completions.size()==1);
    const auto first=original->annotation_completions.front();
    CHECK(first.generation==generation && first.event_id==1 && first.action_log_id==original->last_action_log_id);
    CHECK(first.operation==Op::apply_catalog_profile && first.effect==fake->effect);
    execute(Op::refresh_perf_diagnostics);
    auto current=session.snapshot();
    CHECK(current->last_action_log_id!=first.action_log_id && current->annotation_completions.front().event_id==first.event_id);
    fake->fail_action=true; execute(Op::undo); fake->fail_action=false;
    current=session.snapshot(); CHECK(!current->last_action_ok && current->annotation_completions.size()==2);
    CHECK(current->annotation_completions.back().operation==Op::undo && current->annotation_completions.back().effect);
    CHECK(current->annotation_completions.back().action_log_id==current->last_action_log_id);
    CHECK(session.acknowledge_annotation_completion(generation,2)); // out-of-order
    CHECK(session.acknowledge_annotation_completion(generation,2));
    CHECK(!session.acknowledge_annotation_completion(generation,0));
    CHECK(!session.acknowledge_annotation_completion(generation,3));
    CHECK(!session.acknowledge_annotation_completion(generation+1,1));
    CHECK(session.snapshot()->annotation_completions.size()==1 && original->annotation_completions.size()==1);
    session.set_paused(true); execute(Op::apply_catalog_profile);
    CHECK(session.snapshot()->paused && session.snapshot()->annotation_completions.size()==2);
    const auto before_poll=session.snapshot()->completed_reads;
    session.request_poll(true); Sleep(20);
    CHECK(session.snapshot()->completed_reads==before_poll && session.snapshot()->annotation_completions.size()==2);
    session.set_paused(false);
    session.request_poll(true); CHECK(until([&]{return session.snapshot()->completed_reads>before_poll;}));
    CHECK(session.snapshot()->annotation_completions.size()==2);
    while(session.snapshot()->annotation_completions.size()<Session::annotation_completion_capacity)
        execute(Op::apply_catalog_profile);
    const auto full=session.snapshot();const auto effects=fake->actions.load();
    Command command;command.operation=Op::apply_catalog_profile; CHECK(!session.submit(command,generation));
    command.operation=Op::undo; CHECK(!session.submit(command,generation));
    CHECK(fake->actions.load()==effects && session.snapshot()->annotation_completions.size()==Session::annotation_completion_capacity);
    execute(Op::preflight_catalog_profile); // read-only/ordinary work is not starved
    CHECK(fake->actions.load()==effects+1 && session.snapshot()->annotation_completions.size()==Session::annotation_completion_capacity);
    const auto before_full_poll=session.snapshot()->completed_reads;
    session.request_poll(true); CHECK(until([&]{return session.snapshot()->completed_reads>before_full_poll;}));
    CHECK(session.acknowledge_annotation_completion(generation,first.event_id));
    CHECK(session.acknowledge_annotation_completion(generation,first.event_id));
    execute(Op::undo); CHECK(session.snapshot()->annotation_completions.size()==Session::annotation_completion_capacity);
    CHECK(full->annotation_completions.size()==Session::annotation_completion_capacity);
    CHECK(session.select_device("b"));
    CHECK(until([&]{auto s=session.snapshot();return !s->opening&&s->selected_slot=="b"&&s->data.connected;}));
    CHECK(session.snapshot()->annotation_completions.empty());
    CHECK(!session.acknowledge_annotation_completion(generation,first.event_id));
    generation=session.snapshot()->generation;
    execute(Op::apply_catalog_profile);current=session.snapshot();
    CHECK(current->annotation_completions.size()==1 && current->annotation_completions.front().generation==generation);
    CHECK(current->annotation_completions.front().event_id==1);
    session.begin_shutdown(); CHECK(until([&]{return session.finished();}));
    CHECK(session.snapshot()->annotation_completions.size()==1);
    CHECK(session.acknowledge_annotation_completion(generation,1));
    CHECK(session.snapshot()->annotation_completions.empty());
    std::printf("policy annotation delivery: %u checks\n",checks);
}

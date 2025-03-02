# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]
import copy
import logging
import math
import os
import sys
import tempfile

from model_registry import ModelWithKwargs, MultiMLP, MultiMLPWithDw, MultiMLPWithView
from schedule_registry import (
    ScheduleUnbalanced,
    ScheduleVShaped,
    ScheduleWithReorderedB,
    ScheduleWithW,
)

import torch
import torch.distributed as dist
from torch.distributed.pipelining import (
    _ScheduleForwardOnly,
    pipeline,
    PipelineStage,
    Schedule1F1B,
    ScheduleGPipe,
    ScheduleInterleaved1F1B,
    ScheduleInterleavedZeroBubble,
    ScheduleLoopedBFS,
    ScheduleZBVZeroBubble,
)
from torch.distributed.pipelining.schedules import _PipelineScheduleRuntime
from torch.testing._internal.common_cuda import TEST_MULTIGPU
from torch.testing._internal.common_distributed import (
    MultiProcContinousTest,
    requires_nccl,
)
from torch.testing._internal.common_utils import (
    check_leaked_tensors,
    instantiate_parametrized_tests,
    parametrize,
    skip_but_pass_in_sandcastle_if,
)


logger = logging.getLogger(__name__)

d_hid = 512
batch_size = 256

torch.manual_seed(0)


class ScheduleTest(MultiProcContinousTest):
    @classmethod
    def backend_str(cls) -> str:
        # Testing with NCCL backend
        return "nccl"

    @classmethod
    def setUpClass(cls):
        """
        Class-scope test fixture. Run once for entire test class, before any test starts.
        Set up the device.
        """
        super().setUpClass()
        dev_id = cls.rank % torch.cuda.device_count()
        cls.device = torch.device(f"cuda:{dev_id}")

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [_ScheduleForwardOnly])
    def test_forward_only(self, ScheduleClass):
        mod = MultiMLP(d_hid, n_layers=self.world_size)
        mod.to(self.device)

        mod_ref = copy.deepcopy(mod)

        x = torch.randn(batch_size, d_hid, device=self.device)
        x_clone = x.clone()

        num_microbatches = 4
        x_mb = x.chunk(num_microbatches)[0]

        # Create a pipeline
        split_spec = mod.split_spec if hasattr(mod, "split_spec") else None
        pipe = pipeline(
            mod,
            mb_args=(x_mb,),
            split_spec=split_spec,
        )

        stage = pipe.build_stage(
            self.rank,
            self.device,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, num_microbatches)

        # Run
        num_iters = 20
        for _ in range(num_iters):
            if self.rank == 0:
                schedule.step(x)
                dist.recv(x, src=self.world_size - 1)
            elif self.rank == self.world_size - 1:
                out = schedule.step()
                dist.send(out, dst=0)
            else:
                schedule.step()

        # Validate pipelined output is the same as reference model
        if self.rank == self.world_size - 1:
            for _ in range(num_iters):
                x_clone = mod_ref(x_clone)

            torch.testing.assert_close(x_clone, out)

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    def test_multi_iter(self, ScheduleClass):
        mod = MultiMLP(d_hid, n_layers=self.world_size)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)
        target = torch.randn(batch_size, d_hid, device=self.device)
        loss_fn = torch.nn.MSELoss(reduction="sum")

        chunks = 4
        x_mb = x.chunk(chunks)[0]

        # Create a pipeline
        split_spec = mod.split_spec if hasattr(mod, "split_spec") else None
        pipe = pipeline(
            mod,
            mb_args=(x_mb,),
            split_spec=split_spec,
        )

        stage = pipe.build_stage(
            self.rank,
            self.device,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        for _ in range(20):
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                schedule.step(target=target, losses=losses)
            else:
                schedule.step()

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    def test_kwargs_with_tracer(self, ScheduleClass):
        mod = ModelWithKwargs(d_hid)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)
        y = torch.randn(batch_size, d_hid, device=self.device)
        target = torch.randn(batch_size, d_hid, device=self.device)
        loss_fn = torch.nn.MSELoss(reduction="sum")

        chunks = 4
        x_mb = x.chunk(chunks)[0]
        y_mb = y.chunk(chunks)[0]

        pipe = pipeline(
            mod,
            mb_args=(x_mb,),
            mb_kwargs={"y": y_mb},
        )

        stage = pipe.build_stage(
            self.rank,
            self.device,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        if self.rank == 0:
            schedule.step(x, y=y)
        elif self.rank == self.world_size - 1:
            losses = []
            out = schedule.step(target=target, losses=losses)
        else:
            schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            ref_out = mod(x, y=y)
            ref_loss = loss_fn(ref_out, target)
            pipe_loss = sum(losses)
            torch.testing.assert_close(out, ref_out, rtol=1e-2, atol=5e-3)
            torch.testing.assert_close(pipe_loss, ref_loss)

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    @parametrize("ModelClass", [MultiMLP])
    def test_grad_with_tracer(self, ScheduleClass, ModelClass):
        mod = ModelClass(d_hid)
        mod.to(self.device)

        ref_mod = copy.deepcopy(mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Create a pipeline
        chunks = 4
        x_mb = x.chunk(chunks)[0]
        split_spec = mod.split_spec if hasattr(mod, "split_spec") else None
        pipe = pipeline(
            mod,
            mb_args=(x_mb,),
            split_spec=split_spec,
        )

        stage = pipe.build_stage(
            self.rank,
            self.device,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        stage_module = pipe.get_stage_module(self.rank)
        for _ in range(2):
            # Zero gradients
            stage_module.zero_grad()
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                out = schedule.step(target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for name, p in stage_module.named_parameters():
            ref_p = ref_mod.get_parameter(name)
            try:
                torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
            except AssertionError:
                print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    @parametrize("shape_inference", [True, False])
    def test_grad_with_manual(self, ScheduleClass, shape_inference):
        full_mod = MultiMLP(d_hid, n_layers=self.world_size)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Get a submodule, e.g. `layers.0` or `layers.1`
        submod_name = f"layers.{self.rank}"
        stage_module = full_mod.get_submodule(submod_name)
        chunks = 4

        if shape_inference:
            input_args = None
            output_args = None
        else:
            input_args = (x.chunk(chunks)[0],)
            with torch.no_grad():
                output_args = stage_module(*input_args)

        # Create a pipeline stage to wrap that submodule
        stage = PipelineStage(
            stage_module,
            self.rank,
            self.world_size,
            self.device,
            input_args=input_args,
            output_args=output_args,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        for _ in range(2):
            # Zero gradients
            stage_module.zero_grad()
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                out = schedule.step(target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        ref_submod = ref_mod.get_submodule(submod_name)
        for name, p in stage_module.named_parameters():
            ref_p = ref_submod.get_parameter(name)
            try:
                torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
            except AssertionError:
                print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize(
        "ScheduleClass",
        [
            ScheduleInterleaved1F1B,
            ScheduleLoopedBFS,
            ScheduleInterleavedZeroBubble,
        ],
    )
    @parametrize("use_new_runtime", [False, True])
    def test_grad_with_manual_interleaved(self, ScheduleClass, use_new_runtime):
        stages_per_rank = 2
        n_stages = stages_per_rank * self.world_size
        full_mod = MultiMLP(d_hid, n_layers=n_stages)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Get a submodule, e.g. `layers.0` or `layers.1`
        stage_indices = [
            self.rank + i * self.world_size for i in range(stages_per_rank)
        ]
        print(f"Rank {self.rank} stages: {stage_indices}")
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [
            full_mod.get_submodule(submod_name) for submod_name in submod_names
        ]
        # Create a pipeline stage to wrap that submodule
        num_microbatches = (
            ScheduleClass.num_microbatches
            if hasattr(ScheduleClass, "num_microbatches")
            else 8
        )
        stages = [
            PipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
            )
            for stage_module, stage_idx in zip(stage_modules, stage_indices)
        ]

        # Attach to a schedule
        schedule = ScheduleClass(stages, num_microbatches, loss_fn=loss_fn)
        if use_new_runtime:
            old_schedule = schedule
            tmp_schedule = _PipelineScheduleRuntime(
                stages,
                num_microbatches,
                loss_fn=loss_fn,
                stage_index_to_group_rank=old_schedule.stage_index_to_group_rank,
            )
            tmp_schedule._load_actions(old_schedule.pipeline_order)
            # test that csv round-trip works for compute_comms schedule
            schedule = _PipelineScheduleRuntime(
                stages,
                num_microbatches,
                loss_fn=loss_fn,
                stage_index_to_group_rank=old_schedule.stage_index_to_group_rank,
            )
            with tempfile.NamedTemporaryFile() as f:
                tmp_schedule._dump_csv(f.name)
                f.seek(0)
                schedule._load_csv(f.name, format="compute_comms")
            one_more_schedule = _PipelineScheduleRuntime(
                stages,
                num_microbatches,
                loss_fn=loss_fn,
                stage_index_to_group_rank=old_schedule.stage_index_to_group_rank,
            )
            one_more_schedule._load_actions(
                schedule.pipeline_order_with_comms, format="compute_comms"
            )
            self.assertEqual(
                len(schedule.pipeline_order_with_comms),
                len(
                    one_more_schedule.pipeline_order_with_comms,
                ),
            )
            for rank in schedule.pipeline_order_with_comms:
                self.assertEqual(
                    len(schedule.pipeline_order_with_comms[rank]),
                    len(
                        one_more_schedule.pipeline_order_with_comms[rank],
                    ),
                )
                for a, b in zip(
                    schedule.pipeline_order_with_comms[rank],
                    one_more_schedule.pipeline_order_with_comms[rank],
                ):
                    self.assertEqual(a, b)

        # Run
        with check_leaked_tensors() as garbage_tensors:
            for _ in range(2):
                # Zero gradients
                for stage_module in stage_modules:
                    stage_module.zero_grad()
                if self.rank == 0:
                    schedule.step(x)
                elif self.rank == self.world_size - 1:
                    losses = []
                    out = schedule.step(target=target, losses=losses)
                else:
                    schedule.step()
        self.assertEqual(
            len(garbage_tensors),
            0,
            "Found leaked tensors, check logs above for debug info",
        )
        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for stage_module, submod_name in zip(stage_modules, submod_names):
            # Get corresponding submodule from reference model
            ref_submod = ref_mod.get_submodule(submod_name)
            # Check gradients per parameter
            for name, p in stage_module.named_parameters():
                ref_p = ref_submod.get_parameter(name)
                try:
                    torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
                except AssertionError:
                    print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                    raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleWithW, ScheduleInterleavedZeroBubble])
    def test_schedule_with_native_zero_bubble(self, ScheduleClass):
        print(ScheduleClass)
        if ScheduleClass is ScheduleInterleavedZeroBubble:
            n_stages = 4
            num_microbatches = 8
            rank_stages = {
                0: [0, 2],
                1: [1, 3],
            }
        else:
            n_stages = ScheduleClass.n_stages
            num_microbatches = ScheduleClass.num_microbatches
            rank_stages = ScheduleClass.rank_stages

        num_steps = 4
        full_mod = MultiMLP(d_hid, n_layers=n_stages)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        # x = torch.randn(batch_size, d_hid, device=self.device, requires_grad=True)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Create a pipeline stage to wrap that submodule
        stage_indices = rank_stages[self.rank]
        print(f"Rank {self.rank} stages: {stage_indices}")
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [
            full_mod.get_submodule(submod_name) for submod_name in submod_names
        ]
        stages = [
            PipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
            )
            for stage_module, stage_idx in zip(stage_modules, rank_stages[self.rank])
        ]

        schedule = ScheduleClass(stages, num_microbatches, loss_fn=loss_fn)

        # Run reference
        ref_x = x.detach().clone().requires_grad_(x.requires_grad)
        torch.testing.assert_close(x, ref_x)
        for _ in range(num_steps):
            ref_out = ref_mod(ref_x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        with check_leaked_tensors() as garbage_tensors:
            # Run pipelined stages
            for _ in range(num_steps):
                if self.rank == 0:
                    schedule.step(x)
                elif self.rank == self.world_size - 1:
                    losses = []
                    schedule.step(target=target, losses=losses)
                else:
                    schedule.step()
        self.assertEqual(
            len(garbage_tensors),
            0,
            "Found leaked tensors, check logs above for debug info",
        )

        # Every rank checks parameters compared with the reference model
        for stage_module, submod_name in zip(stage_modules, submod_names):
            # Get corresponding submodule from reference model
            ref_submod = ref_mod.get_submodule(submod_name)
            # Check gradients per parameter
            for name, p in stage_module.named_parameters():
                ref_p = ref_submod.get_parameter(name)
                try:
                    torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
                except AssertionError:
                    print(
                        f"Parameter test failed for {submod_name}.{name}: {p.grad} vs {ref_p.grad}"
                    )
                    raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize(
        "ScheduleClass",
        [
            ScheduleWithReorderedB,
        ],
    )
    def test_pipeline_schedule_runtime_custom_sched(self, ScheduleClass):
        n_stages = 2
        num_microbatches = 2
        stages_per_rank = 1
        full_mod = MultiMLP(d_hid, n_layers=n_stages)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Get a submodule, e.g. `layers.0` or `layers.1`
        stage_indices = [
            self.rank + i * self.world_size for i in range(stages_per_rank)
        ]
        print(f"Rank {self.rank} stages: {stage_indices}")
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [
            full_mod.get_submodule(submod_name) for submod_name in submod_names
        ]
        # Create a pipeline stage to wrap that submodule
        num_microbatches = (
            ScheduleClass.num_microbatches
            if hasattr(ScheduleClass, "num_microbatches")
            else 8
        )
        stages = [
            PipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
            )
            for stage_module, stage_idx in zip(stage_modules, stage_indices)
        ]

        # Attach to a schedule
        schedule = ScheduleClass(stages, num_microbatches, loss_fn=loss_fn)
        assert isinstance(schedule, _PipelineScheduleRuntime)

        # Run
        with check_leaked_tensors() as garbage_tensors:
            for _ in range(2):
                # Zero gradients
                for stage_module in stage_modules:
                    stage_module.zero_grad()
                if self.rank == 0:
                    schedule.step(x)
                elif self.rank == self.world_size - 1:
                    losses = []
                    out = schedule.step(target=target, losses=losses)
                else:
                    schedule.step()
        self.assertEqual(
            len(garbage_tensors),
            0,
            "Found leaked tensors, check logs above for debug info",
        )
        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for stage_module, submod_name in zip(stage_modules, submod_names):
            # Get corresponding submodule from reference model
            ref_submod = ref_mod.get_submodule(submod_name)
            # Check gradients per parameter
            for name, p in stage_module.named_parameters():
                ref_p = ref_submod.get_parameter(name)
                try:
                    torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
                except AssertionError:
                    print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                    raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize(
        "schedule_class", [ScheduleVShaped, ScheduleUnbalanced, ScheduleZBVZeroBubble]
    )
    @parametrize("use_new_runtime", [False, True])
    def test_non_symmetric_stage_ids(self, schedule_class, use_new_runtime):
        if schedule_class is ScheduleZBVZeroBubble:
            n_stages = 4
            rank_stages = {
                0: [0, 3],
                1: [1, 2],
            }
        else:
            n_stages = schedule_class.n_stages
            rank_stages = schedule_class.rank_stages
        full_mod = MultiMLP(d_hid, n_layers=n_stages)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Create a pipeline stage to wrap that submodule
        num_microbatches = 1
        stage_indices = rank_stages[self.rank]
        print(f"Rank {self.rank} stages: {stage_indices}")
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [
            full_mod.get_submodule(submod_name) for submod_name in submod_names
        ]
        stages = [
            PipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
            )
            for stage_module, stage_idx in zip(stage_modules, rank_stages[self.rank])
        ]

        # Attach to a schedule
        stage_index_to_group_rank = {
            value: key for key, values in rank_stages.items() for value in values
        }
        schedule = schedule_class(
            stages,
            num_microbatches,
            stage_index_to_group_rank=stage_index_to_group_rank,
            loss_fn=loss_fn,
        )

        if use_new_runtime:
            old_schedule = schedule
            schedule = _PipelineScheduleRuntime(
                stages,
                num_microbatches,
                loss_fn=loss_fn,
                stage_index_to_group_rank=old_schedule.stage_index_to_group_rank,
            )
            schedule._load_actions(old_schedule.pipeline_order)

        # Run
        # TODO how to better specify .step() when first and last stage are on rank 0...
        for _ in range(2):
            # Zero gradients
            for stage_module in stage_modules:
                stage_module.zero_grad()
            if self.rank == 0:
                losses = []
                out = schedule.step(x, target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == 0:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for stage_module, submod_name in zip(stage_modules, submod_names):
            # Get corresponding submodule from reference model
            ref_submod = ref_mod.get_submodule(submod_name)
            # Check gradients per parameter
            for name, p in stage_module.named_parameters():
                ref_p = ref_submod.get_parameter(name)
                try:
                    torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
                except AssertionError:
                    print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                    raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleInterleavedZeroBubble])
    def test_schedule_with_weight_update_mlp_e2e(self, ScheduleClass):
        stages_per_rank = 2
        n_stages = stages_per_rank * self.world_size
        full_mod = MultiMLPWithDw(d_hid, n_layers=n_stages)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        ref_loss_fn = torch.nn.MSELoss(reduction="sum")
        full_loss_fn = torch.nn.MSELoss(reduction="sum")

        full_mod.toggle()

        # Get a submodule, e.g. `layers.0` or `layers.1`
        stage_indices = [
            self.rank + i * self.world_size for i in range(stages_per_rank)
        ]
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [
            full_mod.get_submodule(submod_name) for submod_name in submod_names
        ]

        # Run reference
        for _ in range(2):
            ref_stage_modules = [
                ref_mod.get_submodule(submod_name) for submod_name in submod_names
            ]
            for stage_module in ref_stage_modules:
                stage_module.zero_grad()

            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = ref_loss_fn(ref_out, target)
            ref_loss.backward()

        class CustomState:
            def __init__(self, stage_module, stage_idx, rank):
                self.i = 0
                self.stage_module = stage_module
                self.stage_idx = stage_idx
                self.rank = rank

            def dw_builder(self):
                def dw_runner():
                    # This inner function would be called by PipelineStage during `backward_weight_one_chunk`
                    self.i += 1
                    print(
                        f"[Rank {self.rank}] dw_count={self.i} stage={self.stage_idx}"
                    )
                    self.stage_module.compute_dW()

                return dw_runner

        cs = {}
        for stage_module, stage_idx in zip(stage_modules, stage_indices):
            cs[stage_idx] = CustomState(stage_module, stage_idx, self.rank)

        # Create a pipeline stage to wrap that submodule
        chunks = 2
        stages = [
            PipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
                dw_builder=cs[stage_idx].dw_builder,
            )
            for stage_module, stage_idx in zip(stage_modules, stage_indices)
        ]

        # Attach to a schedule
        schedule = ScheduleClass(stages, chunks, loss_fn=full_loss_fn)

        for _ in range(2):
            # Zero gradients
            for stage_module in stage_modules:
                stage_module.zero_grad()
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                out = schedule.step(target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()
        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)

            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for stage_module, submod_name in zip(stage_modules, submod_names):
            # Get corresponding submodule from reference model
            ref_submod = ref_mod.get_submodule(submod_name)
            # Check gradients per parameter
            for name, p in stage_module.named_parameters():
                ref_p = ref_submod.get_parameter(name)
                torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    @parametrize("ModelClass", [MultiMLP, MultiMLPWithView])
    @parametrize("LossType", ["mse", "mse_view"])
    def test_schedule_memory_usage(self, ScheduleClass, ModelClass, LossType):
        mod = ModelClass(d_hid, n_layers=self.world_size)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)

        # pre-run to allocate cuBLAS workspaces (see in test_fully_shard_memory.py)
        mod(x).sum().backward()
        torch.cuda.empty_cache()

        x = torch.randn(batch_size, d_hid, device=self.device)
        target = torch.randn(batch_size, d_hid, device=self.device)

        mod.zero_grad(set_to_none=True)  # reset grads memory
        base_mem = self._get_curr_active_memory()

        if LossType == "mse":
            loss_fn = torch.nn.MSELoss(reduction="sum")
        elif LossType == "mse_view":

            def view_loss_fn(x, y):
                return torch.nn.functional.mse_loss(
                    x.view(x.size(0), -1), y.view(y.size(0), -1)
                )

            loss_fn = view_loss_fn

        chunks = 4
        x_mb = x.chunk(chunks)[0]

        my_part = mod.get_submodule(f"layers.{self.rank}")
        my_params_size = (
            sum(p.numel() * p.element_size() for p in my_part.parameters()) / 1e6
        )

        split_spec = mod.split_spec if hasattr(mod, "split_spec") else None
        pipe = pipeline(
            mod,
            mb_args=(x_mb,),
            split_spec=split_spec,
        )

        stage = pipe.build_stage(
            self.rank,
            self.device,
        )

        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        current_mem = self._get_curr_active_memory()
        self.assertLessEqual(
            current_mem,
            base_mem,
            f"Rank {self.rank}: Creating the pipeline should not increase memory usage",
        )

        # Stage uses buffers internally for communications, we have to account for them
        buffer_mem = x.numel() * x.element_size() / 1e6

        # First and last rank only need one buffer (inputs or grads)
        # The other ranks need both
        if self.rank == 0 or self.rank == self.world_size - 1:
            total_buffer_mem = buffer_mem
        else:
            total_buffer_mem = buffer_mem * 2

        for _ in range(5):
            if self.rank == 0:
                schedule.step(x)
                output_mem = 0

            elif self.rank == self.world_size - 1:
                losses = []
                output = schedule.step(target=target, losses=losses)

                output_mem = (output.numel() * output.element_size()) / 1e6

                # Everything should be detached to free memory
                assert output.grad_fn is None
                for loss in losses:
                    assert loss.grad_fn is None
            else:
                schedule.step()
                output_mem = 0

            # these states are cleared at the beginning of each step, but not the end, so we need to clear them here
            stage.clear_runtime_states()

            current_mem = self._get_curr_active_memory()

            expected_mem = math.ceil(
                base_mem + output_mem + my_params_size + total_buffer_mem
            )  # ceil to allow a small margin

            # Gradients were also allocated during backward pass, with size `my_params_size`
            self.assertLessEqual(
                current_mem,
                expected_mem,
                f"Rank {self.rank}: Memory usage should not be increased after the end of backward pass",
            )

        print(
            f"Rank {self.rank} current_mem: {current_mem} ; expected: {expected_mem}"
            + f" ({base_mem} + {output_mem} + {my_params_size} + {total_buffer_mem})"
        )

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleInterleavedZeroBubble])
    def test_schedule_memory_usage_zero_bubble(self, ScheduleClass):
        stages_per_rank = 2
        n_stages = stages_per_rank * self.world_size

        mod = MultiMLP(d_hid, n_layers=n_stages)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)

        # pre-run to allocate cuBLAS workspaces (see in test_fully_shard_memory.py)
        mod(x).sum().backward()
        torch.cuda.empty_cache()

        x = torch.randn(batch_size, d_hid, device=self.device)
        target = torch.randn(batch_size, d_hid, device=self.device)

        mod.zero_grad(set_to_none=True)  # reset grads memory
        base_mem = self._get_curr_active_memory()

        loss_fn = torch.nn.MSELoss(reduction="sum")

        chunks = 4

        stage_indices = [
            self.rank + i * self.world_size for i in range(stages_per_rank)
        ]
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [mod.get_submodule(submod_name) for submod_name in submod_names]

        my_params_size = (
            sum(
                sum(p.numel() * p.element_size() for p in part.parameters())
                for part in stage_modules
            )
            / 1e6
        )

        stages = [
            PipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
            )
            for stage_module, stage_idx in zip(stage_modules, stage_indices)
        ]

        schedule = ScheduleClass(stages, chunks, loss_fn=loss_fn)

        current_mem = self._get_curr_active_memory()
        self.assertLessEqual(
            current_mem,
            base_mem,
            f"Rank {self.rank}: Creating the pipeline should not increase memory usage",
        )

        buffer_mem = x.numel() * x.element_size() / 1e6

        # Each stage needs 2 buffers, except for the first and last that need only one
        total_buffer_mem = buffer_mem * stages_per_rank * 2
        if self.rank == 0 or self.rank == self.world_size - 1:
            total_buffer_mem -= buffer_mem

        for _ in range(5):
            if self.rank == 0:
                schedule.step(x)
                output_mem = 0
            elif self.rank == self.world_size - 1:
                losses = []
                output = schedule.step(target=target, losses=losses)

                output_mem = (output.numel() * output.element_size()) / 1e6

                # Everything should be detached to free memory
                assert output.grad_fn is None
                for loss in losses:
                    assert loss.grad_fn is None
            else:
                schedule.step()
                output_mem = 0

            # these states are cleared at the beginning of each step, but not the end, so we need to clear them here
            for stage in stages:
                stage.clear_runtime_states()

            current_mem = self._get_curr_active_memory()

            expected_mem = math.ceil(
                base_mem + output_mem + my_params_size + total_buffer_mem
            )

            # Gradients were also allocated during backward pass, with size `my_params_size`
            self.assertLessEqual(
                current_mem,
                expected_mem,
                f"Rank {self.rank}: Memory usage should not be increased after the end of backward pass",
            )

        print(
            f"Rank {self.rank} current_mem: {current_mem} ; expected: {expected_mem}"
            + f" ({base_mem} + {output_mem} + {my_params_size} + {total_buffer_mem}"
        )

    def _get_curr_active_memory(self) -> int:
        mem_stats = torch.cuda.memory_stats(self.device)
        return round(mem_stats["allocated_bytes.all.current"] / 1e6)


instantiate_parametrized_tests(ScheduleTest)


if __name__ == "__main__":
    # Check if GPU and NCCL are available
    if not (
        dist.is_available()
        and dist.is_nccl_available()
        and torch.cuda.device_count() > 1
    ):
        print(
            "c10d NCCL not available or not enough GPUs, skipping tests",
            file=sys.stderr,
        )
        sys.exit(0)

    rank = int(os.getenv("RANK", -1))
    world_size = int(os.getenv("WORLD_SIZE", 2))

    if rank != -1:
        # Launched with torchrun or other multi-proc launchers. Directly run the test.
        ScheduleTest.run_rank(rank, world_size)
    else:
        # Launched as a single process. Spawn subprocess to run the tests.
        # Also need a rendezvous file for `init_process_group` purpose.
        rdvz_file = tempfile.NamedTemporaryFile(delete=False).name
        torch.multiprocessing.spawn(
            ScheduleTest.run_rank,
            nprocs=world_size,
            args=(world_size, rdvz_file),
        )

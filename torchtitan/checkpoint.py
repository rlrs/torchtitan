# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.

import enum
import os
import re
import time
from typing import Any, Dict

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
import torch.nn as nn
from torch.distributed.checkpoint.state_dict import (
    get_model_state_dict,
    get_optimizer_state_dict,
    set_model_state_dict,
    set_optimizer_state_dict,
)
from torchtitan.logging_utils import logger


class IntervalType(enum.Enum):
    SECONDS = enum.auto()
    STEPS = enum.auto()


class ModelWrapper:
    def __init__(self, model: nn.Module) -> None:
        self.model = model

    def state_dict(self) -> None:
        return get_model_state_dict(self.model)

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        set_model_state_dict(self.model, state_dict)


class OptimizerWrapper:
    def __init__(self, model: nn.Module, optim: torch.optim.Optimizer) -> None:
        self.model = model
        self.optim = optim

    def state_dict(self) -> None:
        return get_optimizer_state_dict(self.model, self.optim)

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        set_optimizer_state_dict(self.model, self.optim, optim_state_dict=state_dict)


class CheckpointManager:
    def __init__(
        self,
        model: nn.Module,
        optimizer: torch.optim.Optimizer,
        states: Dict[str, Any],
        folder: str,
        interval_type: IntervalType,
        interval: int,
        model_weights_only: bool = False,
    ) -> None:
        self.folder = folder
        self.states = states
        self.model_weights_only = model_weights_only
        self.states.update(
            {
                "model": ModelWrapper(model),
                "optimizer": OptimizerWrapper(model, optimizer),
            }
        )
        self.interval_type = interval_type
        self.interval = interval
        self.begin = 0
        self.work = None
        self.pg = dist.new_group(backend="gloo")
        self.doit = None

        if self.folder:
            logger.info(
                f"Checkpointing active. Checkpoints will be loaded from and saved to {self.folder}"
            )

    def reset(self) -> None:
        self.begin = time.monotonic()

    def create_checkpoint_id(self, step: int) -> str:
        return os.path.join(self.folder, f"step-{step}")

    def save(self, curr_step: int, force: bool = False) -> None:
        """
        force = True will force the checkpoint to be saved, even if the interval has not been reached.
        This only happens when train_state.step == job_config.training.steps.
        """
        if not self.folder:
            return

        if not force:
            if self.interval_type == IntervalType.STEPS and not (
                curr_step % self.interval == 0
            ):
                return
            if self.interval_type == IntervalType.SECONDS:
                doit = (time.monotonic() - self.begin) >= self.interval
                self.doit = torch.tensor(int(doit))
                if self.work is None:
                    self.work = dist.all_reduce(self.doit, group=self.pg, async_op=True)
                    return
                elif curr_step % 5 == 4:
                    self.work.wait()
                    self.work = None
                    doit = self.doit.item()
                    self.doit = None
                    if doit == 0:
                        return
                else:
                    return

        if self.work:
            self.work.wait()
            self.work = None
            self.doit = None

        # We only consider saving weights only at the end of the training.
        # So this won't affect preemption and training resume.
        weights_only = force and self.model_weights_only
        weights_only_msg = (
            "Saving model weights only" if weights_only else "Saving a full checkpoint"
        )
        self.states = {"model": self.states["model"]} if weights_only else self.states
        logger.info(f"{weights_only_msg} at step {curr_step}")

        begin = time.monotonic()
        dcp.save(self.states, checkpoint_id=self.create_checkpoint_id(curr_step))
        self.reset()
        logger.info(
            f"Finished saving the checkpoint in {time.monotonic() - begin:.2f} seconds"
        )

    def load(self, step: int = -1) -> bool:
        if not self.folder:
            return False
        if not os.path.isdir(self.folder):
            return False
        if step != -1 and not os.path.isdir(self.create_checkpoint_id(step)):
            return False

        if step == -1:
            step_counts = []
            for filename in os.listdir(self.folder):
                match = re.search(r"step-(\d+)", filename)
                if match:
                    step_counts.append(int(match.group(1)))
            if not step_counts:
                return False
            step = max(step_counts)

        logger.info(f"Loading the checkpoint at step {step}")
        begin = time.monotonic()
        dcp.load(
            self.states,
            checkpoint_id=self.create_checkpoint_id(step),
        )
        logger.info(
            f"Finished loading the checkpoint in {time.monotonic() - begin:.2f} seconds"
        )
        return True
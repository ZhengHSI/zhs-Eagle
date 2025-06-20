# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import torch
import torch.nn.functional as F
from torch import nn
from torch.distributed.tensor import DTensor

from torchtune.modules.loss.loss_types import SFTLoss
from torchtune.utils import get_logger

log = get_logger()


class DraftLoss(nn.Module, SFTLoss):
    """Memory efficient Cross-entropy loss that incrementally computes loss for chunks of tokens
    by masking ignored tokens, calculating logits and then applying cross-entropy loss. Combines
    the linear projection with the cross-entropy calculation for further memory savings.

    Linear cross entropy masks out ignored tokens before the projection layer to save memory.
    You therefore need to skip the final projection layer in your model and pass it to the loss instead.
    You can setup the loss with the model and compile it as shown below.

    >>> model = Transformer(...)
    >>> loss = LinearCrossEntropyLoss(...)
    >>> loss.set_model_output(model)
    >>> loss.apply_compile_strategy()
    """

    def __init__(
        self,
        num_output_chunks: int = 8,
        ignore_index: int = -100,
    ):
        super().__init__()
        """
        Args:
            num_output_chunks (int): Number of chunks to split the output tensor into. Default is 8.
            ignore_index (int): Index to ignore in the target tensor. Default is -100.
        """
        self.linear_projection = None
        self.num_output_chunks = num_output_chunks
        self.ignore_index = ignore_index

    def set_model_output(self, model: nn.Module) -> None:
        """Modify model output to match the expected input for the loss function."""
        model.skip_output_layer = True
        self.linear_projection = model.output

    def compute_draft_loss(
        self,
        backbone_output_hidden_states: torch.Tensor,
        draft_output_hidden_states: torch.Tensor,
        loss_mask: torch.Tensor,
    ) -> torch.Tensor:
        if loss_mask.sum() == 0:
            loss_mask[0] = True
        
        # Select hidden states and targets where mask is True
        if isinstance(backbone_output_hidden_states, DTensor):
            # DTensor doesn't support masks so we have to mask locally
            mesh = backbone_output_hidden_states.device_mesh
            placements = backbone_output_hidden_states.placements
            local_backbone_output_hidden_states = backbone_output_hidden_states.to_local()[loss_mask]
            backbone_output_hidden_states = DTensor.from_local(
                local_backbone_output_hidden_states, mesh, placements
            )  # [num_valid, embed_dim]
        else:
            backbone_output_hidden_states = backbone_output_hidden_states[loss_mask]  # [num_valid, embed_dim]

         # Select hidden states and targets where mask is True
        if isinstance(draft_output_hidden_states, DTensor):
            # DTensor doesn't support masks so we have to mask locally
            mesh = draft_output_hidden_states.device_mesh
            placements = draft_output_hidden_states.placements
            local_draft_output_hidden_states = draft_output_hidden_states.to_local()[loss_mask]
            draft_output_hidden_states = DTensor.from_local(
                local_draft_output_hidden_states, mesh, placements
            )  # [num_valid, embed_dim]
        else:
            draft_output_hidden_states = draft_output_hidden_states[loss_mask]  # [num_valid, embed_dim]

        # [num_valid, embed_dim] @ [embed_dim, vocab_size]
        if self.linear_projection is None:
            raise AttributeError("forward called before update_model")
        
        # backbone model logic
        with torch.no_grad():
            backbone_head_out = self.linear_projection(backbone_output_hidden_states)  # [num_valid, vocab_size]
            if isinstance(backbone_head_out, DTensor):
                backbone_head_out = backbone_head_out.full_tensor()
            backbone_probs = nn.Softmax(dim=1)(backbone_head_out).detach()  # [num_valid, vocab_size]
        
        # draft model logic
        draft_head_out = self.linear_projection(draft_output_hidden_states)  # [num_valid, vocab_size]  
        if isinstance(draft_head_out, DTensor):
            draft_head_out = draft_head_out.full_tensor()
        draft_probs = nn.LogSoftmax(dim=1)(draft_head_out).detach()  # [num_valid, vocab_size]
        
        loss_class = backbone_probs * draft_probs  # [num_valid, vocab_size]
        loss_class = -torch.sum(torch.sum(loss_class, 1)) / (
            loss_mask.sum() + 1e-5
        )
        
        loss_reg = nn.SmoothL1Loss(reduction="none")(
            draft_output_hidden_states, backbone_output_hidden_states
        )
        loss_reg = torch.sum(torch.mean(loss_reg, 1)) / (
            loss_mask.sum() + 1e-5
        )
        
        total_loss = (
            loss_reg + (0.1 * loss_class)
        )

        return total_loss

    def forward(
        self,
        backbone_output_hidden_states: torch.Tensor,
        draft_output_hidden_states: torch.Tensor,
        targets: torch.Tensor,
    ) -> torch.Tensor:

        # Total number of non-ignored tokens across the entire batch
        mask = targets != self.ignore_index
        total_elements = mask.sum()

        # Compute cross-entropy loss for the chunks
        total_loss = self.compute_draft_loss(
            backbone_output_hidden_states,
            draft_output_hidden_states,
            mask,
        )

        if total_elements == 0:
            # must return after calling compute_cross_entropy to not hang during data parallel training
            return total_loss
        else:
            return total_loss / total_elements
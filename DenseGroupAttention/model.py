import math
import warnings
from typing import List, Optional, Tuple, Union

import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from torch import nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from transformers import PretrainedConfig
from transformers.activations import ACT2FN
from transformers.cache_utils import Cache, DynamicCache
from transformers.generation.utils import GenerationMixin
from transformers.modeling_attn_mask_utils import (
	AttentionMaskConverter,
	_prepare_4d_attention_mask,
	_prepare_4d_causal_attention_mask,
)
from transformers.modeling_outputs import (
	BaseModelOutputWithPast,
	CausalLMOutputWithPast,
	SequenceClassifierOutputWithPast,
)
from transformers.modeling_utils import PreTrainedModel
from transformers.pytorch_utils import (
	ALL_LAYERNORM_LAYERS,
	is_torch_greater_or_equal_than_1_13,
)
from transformers.utils import (
	add_start_docstrings,
	add_start_docstrings_to_model_forward,
	is_flash_attn_2_available,
	is_flash_attn_greater_or_equal_2_10,
	logging,
	replace_return_docstrings,
)
from transformers.utils.import_utils import is_torch_fx_available
from configuration_deepseek import DeepseekV2Config
import torch.distributed as dist
import numpy as np

if is_flash_attn_2_available():
	from flash_attn import flash_attn_func, flash_attn_varlen_func
	from flash_attn.bert_padding import index_first_axis, pad_input, unpad_input  # noqa


# This makes `_prepare_4d_causal_attention_mask` a leaf function in the FX graph.
# It means that the function will not be traced through and simply appear as a node in the graph.
if is_torch_fx_available():
	if not is_torch_greater_or_equal_than_1_13:
		import torch.fx

	_prepare_4d_causal_attention_mask = torch.fx.wrap(_prepare_4d_causal_attention_mask)


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "DeepseekV2Config"

torch.set_default_dtype(torch.float16)
xsize=512
#sizelist=[xsize*2,xsize*2,xsize*2,xsize,xsize,xsize,xsize,xsize,xsize,xsize,xsize,xsize,xsize*4,xsize*4,xsize*2,xsize*2,xsize*2,xsize,xsize,xsize,xsize*8,xsize*4,xsize*2,xsize*2,xsize,xsize]
sizelist=[1,1*4,1*2,1*2,1*2,1,1,1,1,1*4,1*2,1*2,1,1]

class newConfig(PretrainedConfig):
	#model_type = "gpt_neox"
	keys_to_ignore_at_inference = ["past_key_values"]
	def __init__(
		self,
		#training=False,
		vocab_size=50304,
		hidden_size=512,
		num_hidden_layers=14,
		num_attention_heads=16,
		head_size=32,
		intermediate_size=1024,
		rotary_emb_base=10000,
		attention_dropout=0.0,
		hidden_dropout=0.0,
		classifier_dropout=0.1,
		max_position_embeddings=1024,
		initializer_range=0.02,
		layer_norm_eps=1e-5,
		use_cache=True,
		bos_token_id=0,
		eos_token_id=2,
		tie_word_embeddings=False,
		use_parallel_residual=False,
		rope_scaling=None,
		attention_bias=True,
		torch_dtype=torch.float16,
		
		moe_intermediate_size = 256,
		num_key_value_heads=32,
		n_shared_experts = 1,
		n_routed_experts = 8,
		ep_size = 1,
		routed_scaling_factor = 1.0,
		kv_lora_rank = 64,
		q_lora_rank = 192,
		qk_rope_head_dim = 16,
		v_head_dim = 32,
		qk_nope_head_dim = 32,
		topk_method = 'gready',
		n_group = None,
		topk_group = None,
		num_experts_per_tok = 2,
		moe_layer_freq = 1,
		first_k_dense_replace = 1,
		norm_topk_prob = False,
		scoring_func = 'softmax',
		aux_loss_alpha = 0.001,
		seq_aux = True,
		hidden_act="silu",
		rms_norm_eps=1e-6,
		pretraining_tp=1,
		rope_theta=10000.0,
		**kwargs,
	):
		super().__init__(bos_token_id=bos_token_id, eos_token_id=eos_token_id, **kwargs)
		#self.training = training
		#self.return_dict = False
		self.vocab_size = vocab_size
		self.max_position_embeddings = max_position_embeddings
		self.hidden_size = hidden_size
		self.num_hidden_layers = num_hidden_layers
		self.num_attention_heads = num_attention_heads
		self.head_size = head_size
		self.intermediate_size = intermediate_size
		self.hidden_act = hidden_act
		self.rotary_emb_base = rotary_emb_base
		self.rope_theta = rotary_emb_base
		self.attention_dropout = attention_dropout
		self.hidden_dropout = hidden_dropout
		self.classifier_dropout = classifier_dropout
		self.initializer_range = initializer_range
		self.layer_norm_eps = layer_norm_eps
		self.use_cache = use_cache
		self.tie_word_embeddings = tie_word_embeddings
		self.use_parallel_residual = use_parallel_residual
		self.rope_scaling = rope_scaling
		self.attention_bias = attention_bias
		self.torch_dtype=torch_dtype
		
		self.moe_intermediate_size = moe_intermediate_size
		self.n_shared_experts = n_shared_experts
		self.n_routed_experts = n_routed_experts
		self.ep_size = ep_size
		self.routed_scaling_factor = routed_scaling_factor
		self.kv_lora_rank = kv_lora_rank
		self.q_lora_rank = q_lora_rank
		self.qk_rope_head_dim = qk_rope_head_dim
		self.v_head_dim = v_head_dim
		self.qk_nope_head_dim = qk_nope_head_dim
		self.topk_method = topk_method
		self.n_group = n_group
		self.topk_group = topk_group
		self.num_experts_per_tok = num_experts_per_tok
		self.moe_layer_freq = moe_layer_freq
		self.first_k_dense_replace = first_k_dense_replace
		self.norm_topk_prob = norm_topk_prob
		self.scoring_func = scoring_func
		self.aux_loss_alpha = aux_loss_alpha
		self.seq_aux = seq_aux
		# for backward compatibility
		if num_key_value_heads is None:
			num_key_value_heads = num_attention_heads

		self.num_key_value_heads = num_key_value_heads
		self.hidden_act = hidden_act
		self.initializer_range = initializer_range
		self.rms_norm_eps = rms_norm_eps
		self.pretraining_tp = pretraining_tp
		self.use_cache = use_cache
		self.rope_theta = rope_theta
		self.rope_scaling = rope_scaling
		self.attention_bias = attention_bias
		self.attention_dropout = attention_dropout
		
def _get_unpad_data(attention_mask):
	seqlens_in_batch = attention_mask.sum(dim=-1, dtype=torch.int32)
	indices = torch.nonzero(attention_mask.flatten(), as_tuple=False).flatten()
	max_seqlen_in_batch = seqlens_in_batch.max().item()
	cu_seqlens = F.pad(
		torch.cumsum(seqlens_in_batch, dim=0, dtype=torch.torch.int32), (1, 0)
	)
	return (
		indices,
		cu_seqlens,
		max_seqlen_in_batch,
	)


class DeepseekV2RMSNorm(nn.Module):
	def __init__(self, hidden_size, eps=1e-6):
		"""
		DeepseekV2RMSNorm is equivalent to T5LayerNorm
		"""
		super().__init__()
		self.weight = nn.Parameter(torch.ones(hidden_size))
		self.variance_epsilon = eps

	def forward(self, hidden_states):
		input_dtype = hidden_states.dtype
		hidden_states = hidden_states.to(torch.float32)
		variance = hidden_states.pow(2).mean(-1, keepdim=True)
		hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
		return self.weight * hidden_states.to(input_dtype)


ALL_LAYERNORM_LAYERS.append(DeepseekV2RMSNorm)


class DeepseekV2RotaryEmbedding(nn.Module):
	def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None):
		super().__init__()

		self.dim = dim
		self.max_position_embeddings = max_position_embeddings
		self.base = base
		inv_freq = 1.0 / (
			self.base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim)
		)
		self.register_buffer("inv_freq", inv_freq, persistent=False)

		# Build here to make `torch.jit.trace` work.
		self._set_cos_sin_cache(
			seq_len=max_position_embeddings,
			device=self.inv_freq.device,
			dtype=torch.get_default_dtype(),
		)
		self.max_seq_len_cached = None

	def _set_cos_sin_cache(self, seq_len, device, dtype):
		self.max_seq_len_cached = seq_len
		t = torch.arange(
			self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype
		)

		freqs = torch.outer(t, self.inv_freq.to(t.device))
		# Different from paper, but it uses a different permutation in order to obtain the same calculation
		emb = torch.cat((freqs, freqs), dim=-1)
		self.register_buffer("cos_cached", emb.cos().to(dtype), persistent=False)
		self.register_buffer("sin_cached", emb.sin().to(dtype), persistent=False)

	def forward(self, x, seq_len=None):
		# x: [bs, num_attention_heads, seq_len, head_size]
		if self.max_seq_len_cached is None or seq_len > self.max_seq_len_cached:
			self._set_cos_sin_cache(seq_len=seq_len, device=x.device, dtype=x.dtype)

		return (
			self.cos_cached[:seq_len].to(dtype=x.dtype),
			self.sin_cached[:seq_len].to(dtype=x.dtype),
		)


# Copied from transformers.models.llama.modeling_llama.rotate_half
def rotate_half(x):
	"""Rotates half the hidden dims of the input."""
	x1 = x[..., : x.shape[-1] // 2]
	x2 = x[..., x.shape[-1] // 2 :]
	return torch.cat((-x2, x1), dim=-1)


# Copied from transformers.models.llama.modeling_llama.apply_rotary_pos_emb
def apply_rotary_pos_emb(q, k, cos, sin, position_ids, unsqueeze_dim=1):
	"""Applies Rotary Position Embedding to the query and key tensors.

	Args:
		q (`torch.Tensor`): The query tensor.
		k (`torch.Tensor`): The key tensor.
		cos (`torch.Tensor`): The cosine part of the rotary embedding.
		sin (`torch.Tensor`): The sine part of the rotary embedding.
		position_ids (`torch.Tensor`):
			The position indices of the tokens corresponding to the query and key tensors. For example, this can be
			used to pass offsetted position ids when working with a KV-cache.
		unsqueeze_dim (`int`, *optional*, defaults to 1):
			The 'unsqueeze_dim' argument specifies the dimension along which to unsqueeze cos[position_ids] and
			sin[position_ids] so that they can be properly broadcasted to the dimensions of q and k. For example, note
			that cos[position_ids] and sin[position_ids] have the shape [batch_size, seq_len, head_dim]. Then, if q and
			k have the shape [batch_size, heads, seq_len, head_dim], then setting unsqueeze_dim=1 makes
			cos[position_ids] and sin[position_ids] broadcastable to the shapes of q and k. Similarly, if q and k have
			the shape [batch_size, seq_len, heads, head_dim], then set unsqueeze_dim=2.
	Returns:
		`tuple(torch.Tensor)` comprising of the query and key tensors rotated using the Rotary Position Embedding.
	"""
	#size = q.shape[-2]
	#cos, sin = cos[:, :size,...], sin[:, :size,...]
	cos = cos[position_ids].unsqueeze(unsqueeze_dim)
	sin = sin[position_ids].unsqueeze(unsqueeze_dim)

	b, h, s, d = q.shape
	q = q.view(b, h, s, d // 2, 2).transpose(4, 3).reshape(b, h, s, d)

	b, h, s, d = k.shape
	k = k.view(b, h, s, d // 2, 2).transpose(4, 3).reshape(b, h, s, d)

	q_embed = (q * cos) + (rotate_half(q) * sin)
	k_embed = (k * cos) + (rotate_half(k) * sin)
	return q_embed, k_embed


class DeepseekV2MLP(nn.Module):
	def __init__(self, config, size=None, intermediate_size=None):
		super().__init__()
		self.config = config
		self.hidden_size = config.hidden_size if size is None else size
		self.intermediate_size = (
			config.intermediate_size if intermediate_size is None else intermediate_size
		)

		self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
		self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
		self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False)
		self.act_fn = ACT2FN[config.hidden_act]

	def forward(self, x):
		down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))
		return down_proj


class MoEGate(nn.Module):
	def __init__(self, config, size):
		super().__init__()
		self.config = config
		self.top_k = config.num_experts_per_tok
		self.n_routed_experts = config.n_routed_experts
		self.routed_scaling_factor = config.routed_scaling_factor
		self.scoring_func = config.scoring_func
		self.alpha = config.aux_loss_alpha
		self.seq_aux = config.seq_aux
		self.topk_method = config.topk_method
		self.n_group = config.n_group
		self.topk_group = config.topk_group

		# topk selection algorithm
		self.norm_topk_prob = config.norm_topk_prob
		self.gating_dim = size
		self.weight = nn.Parameter(
			torch.empty((self.n_routed_experts, self.gating_dim))
		)
		self.reset_parameters()

	def reset_parameters(self) -> None:
		import torch.nn.init as init

		init.kaiming_uniform_(self.weight, a=math.sqrt(5))

	def forward(self, hidden_states):
		bsz, seq_len, h = hidden_states.shape
		### compute gating score
		hidden_states = hidden_states.view(-1, h)
		logits = F.linear(
			hidden_states.type(torch.float32), self.weight.type(torch.float32), None
		)
		if self.scoring_func == "softmax":
			scores = logits.softmax(dim=-1, dtype=torch.float32)
		else:
			raise NotImplementedError(
				f"insupportable scoring function for MoE gating: {self.scoring_func}"
			)

		### select top-k experts
		#if self.topk_method == "greedy":
		topk_weight, topk_idx = torch.topk(
			scores, k=self.top_k, dim=-1, sorted=False
		)
		if self.topk_method == "group_limited_greedy":
			group_scores = (
				scores.view(bsz * seq_len, self.n_group, -1).max(dim=-1).values
			)  # [n, n_group]
			group_idx = torch.topk(
				group_scores, k=self.topk_group, dim=-1, sorted=False
			)[
				1
			]  # [n, top_k_group]
			group_mask = torch.zeros_like(group_scores)	 # [n, n_group]
			group_mask.scatter_(1, group_idx, 1)  # [n, n_group]
			score_mask = (
				group_mask.unsqueeze(-1)
				.expand(
					bsz * seq_len, self.n_group, self.n_routed_experts // self.n_group
				)
				.reshape(bsz * seq_len, -1)
			)  # [n, e]
			tmp_scores = scores.masked_fill(~score_mask.bool(), 0.0)  # [n, e]
			topk_weight, topk_idx = torch.topk(
				tmp_scores, k=self.top_k, dim=-1, sorted=False
			)

		### norm gate to sum 1
		if self.top_k > 1 and self.norm_topk_prob:
			denominator = topk_weight.sum(dim=-1, keepdim=True) + 1e-20
			topk_weight = topk_weight / denominator
		else:
			topk_weight = topk_weight * self.routed_scaling_factor
		### expert-level computation auxiliary loss
		if self.training and self.alpha > 0.0:
			scores_for_aux = scores
			aux_topk = self.top_k
			# always compute aux loss based on the naive greedy topk method
			topk_idx_for_aux_loss = topk_idx.view(bsz, -1)
			if self.seq_aux:
				scores_for_seq_aux = scores_for_aux.view(bsz, seq_len, -1)
				ce = torch.zeros(
					bsz, self.n_routed_experts, device=hidden_states.device
				)
				ce.scatter_add_(
					1,
					topk_idx_for_aux_loss,
					torch.ones(bsz, seq_len * aux_topk, device=hidden_states.device),
				).div_(seq_len * aux_topk / self.n_routed_experts)
				aux_loss = (ce * scores_for_seq_aux.mean(dim=1)).sum(
					dim=1
				).mean() * self.alpha
			else:
				mask_ce = F.one_hot(
					topk_idx_for_aux_loss.view(-1), num_classes=self.n_routed_experts
				)
				ce = mask_ce.float().mean(0)
				Pi = scores_for_aux.mean(0)
				fi = ce * self.n_routed_experts
				aux_loss = (Pi * fi).sum() * self.alpha
		else:
			aux_loss = None
		return topk_idx, topk_weight, aux_loss


class AddAuxiliaryLoss(torch.autograd.Function):
	"""
	The trick function of adding auxiliary (aux) loss,
	which includes the gradient of the aux loss during backpropagation.
	"""

	@staticmethod
	def forward(ctx, x, loss):
		assert loss.numel() == 1
		ctx.dtype = loss.dtype
		ctx.required_aux_loss = loss.requires_grad
		return x

	@staticmethod
	def backward(ctx, grad_output):
		grad_loss = None
		if ctx.required_aux_loss:
			grad_loss = torch.ones(1, dtype=ctx.dtype, device=grad_output.device)
		return grad_output, grad_loss


class DeepseekV2MoE(nn.Module):
	"""
	A mixed expert module containing shared experts.
	"""

	def __init__(self, config, size):
		super().__init__()
		self.config = config
		self.num_experts_per_tok = config.num_experts_per_tok

		if hasattr(config, "ep_size") and config.ep_size > 1:
			assert config.ep_size == dist.get_world_size()
			self.ep_size = config.ep_size
			self.experts_per_rank = config.n_routed_experts // config.ep_size
			self.ep_rank = dist.get_rank()
			self.experts = nn.ModuleList(
				[
					(
						DeepseekV2MLP(
							config, size, intermediate_size=config.moe_intermediate_size
						)
						if i >= self.ep_rank * self.experts_per_rank
						and i < (self.ep_rank + 1) * self.experts_per_rank
						else None
					)
					for i in range(config.n_routed_experts)
				]
			)
		else:
			self.ep_size = 1
			self.experts_per_rank = config.n_routed_experts
			self.ep_rank = 0
			self.experts = nn.ModuleList(
				[
					DeepseekV2MLP(
						config, size=size, intermediate_size=config.moe_intermediate_size
					)
					for i in range(config.n_routed_experts)
				]
			)
		self.gate = MoEGate(config, size)
		if config.n_shared_experts is not None:
			intermediate_size = config.moe_intermediate_size * config.n_shared_experts
			self.shared_experts = DeepseekV2MLP(
				config=config, size=size, intermediate_size=intermediate_size
			)

	def forward(self, hidden_states):
		identity = hidden_states
		orig_shape = hidden_states.shape
		topk_idx, topk_weight, aux_loss = self.gate(hidden_states)
		hidden_states = hidden_states.view(-1, hidden_states.shape[-1])
		flat_topk_idx = topk_idx.view(-1)
		if self.training:
			hidden_states = hidden_states.repeat_interleave(
				self.num_experts_per_tok, dim=0
			)
			y = torch.empty_like(hidden_states)
			for i, expert in enumerate(self.experts):
				y[flat_topk_idx == i] = expert(hidden_states[flat_topk_idx == i])
			y = (y.view(*topk_weight.shape, -1) * topk_weight.unsqueeze(-1)).sum(dim=1)
			y = y.to(hidden_states.dtype).view(*orig_shape)
			y = AddAuxiliaryLoss.apply(y, aux_loss)
		else:
			y = self.moe_infer(hidden_states, topk_idx, topk_weight).view(*orig_shape)
		if self.config.n_shared_experts is not None:
			y = y + self.shared_experts(identity)
		return y

	@torch.no_grad()
	def moe_infer(self, x, topk_ids, topk_weight):
		cnts = topk_ids.new_zeros((topk_ids.shape[0], len(self.experts)))
		cnts.scatter_(1, topk_ids, 1)
		tokens_per_expert = cnts.sum(dim=0)
		idxs = topk_ids.view(-1).argsort()
		sorted_tokens = x[idxs // topk_ids.shape[1]]
		sorted_tokens_shape = sorted_tokens.shape
		#print(topk_ids.shape,idxs.shape,sorted_tokens_shape)
		if self.ep_size > 1:
			tokens_per_ep_rank = tokens_per_expert.view(self.ep_size, -1).sum(dim=1)
			tokens_per_expert_group = tokens_per_expert.new_empty(
				tokens_per_expert.shape[0]
			)
			dist.all_to_all_single(tokens_per_expert_group, tokens_per_expert)
			output_splits = (
				tokens_per_expert_group.view(self.ep_size, -1)
				.sum(1)
				.cpu()
				.numpy()
				.tolist()
			)
			gathered_tokens = sorted_tokens.new_empty(
				tokens_per_expert_group.sum(dim=0).cpu().item(), sorted_tokens.shape[1]
			)
			input_split_sizes = tokens_per_ep_rank.cpu().numpy().tolist()
			dist.all_to_all(
				list(gathered_tokens.split(output_splits)),
				list(sorted_tokens.split(input_split_sizes)),
			)
			tokens_per_expert_post_gather = tokens_per_expert_group.view(
				self.ep_size, self.experts_per_rank
			).sum(dim=0)
			gatherd_idxs = np.zeros(shape=(gathered_tokens.shape[0],), dtype=np.int32)
			s = 0
			for i, k in enumerate(tokens_per_expert_group.cpu().numpy()):
				gatherd_idxs[s : s + k] = i % self.experts_per_rank
				s += k
			gatherd_idxs = gatherd_idxs.argsort()
			sorted_tokens = gathered_tokens[gatherd_idxs]
			tokens_per_expert = tokens_per_expert_post_gather
		tokens_per_expert = tokens_per_expert.cpu().numpy()

		outputs = []
		start_idx = 0
		for i, num_tokens in enumerate(tokens_per_expert):
			end_idx = start_idx + num_tokens
			if num_tokens == 0:
				continue
			expert = self.experts[i + self.ep_rank * self.experts_per_rank]
			tokens_for_this_expert = sorted_tokens[start_idx:end_idx]
			expert_out = expert(tokens_for_this_expert)
			outputs.append(expert_out)
			start_idx = end_idx

		outs = torch.cat(outputs, dim=0) if len(outputs) else sorted_tokens.new_empty(0)
		if self.ep_size > 1:
			new_x = torch.empty_like(outs)
			new_x[gatherd_idxs] = outs
			gathered_tokens = new_x.new_empty(*sorted_tokens_shape)
			dist.all_to_all(
				list(gathered_tokens.split(input_split_sizes)),
				list(new_x.split(output_splits)),
			)
			outs = gathered_tokens

		new_x = torch.empty_like(outs)
		new_x[idxs] = outs
		final_out = (
			new_x.view(*topk_ids.shape, -1)
			.type(topk_weight.dtype)
			.mul_(topk_weight.unsqueeze(dim=-1))
			.sum(dim=1)
			.type(new_x.dtype)
		)
		return final_out


# Copied from transformers.models.llama.modeling_llama.repeat_kv
def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
	"""
	This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
	num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
	"""
	batch, num_key_value_heads, slen, head_dim = hidden_states.shape
	if n_rep == 1:
		return hidden_states
	hidden_states = hidden_states[:, :, None, :, :].expand(
		batch, num_key_value_heads, n_rep, slen, head_dim
	)
	return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


# Copied from transformers.models.llama.modeling_llama.LlamaAttention with Llama->DeepseekV2
class DeepseekV2Attention(nn.Module):
	"""Multi-headed attention from 'Attention Is All You Need' paper"""

	def __init__(self, config: DeepseekV2Config, layer_idx: Optional[int] = None, mulx=1):
		super().__init__()
		self.config = config
		self.layer_idx = layer_idx
		if layer_idx is None:
			logger.warning_once(
				f"Instantiating {self.__class__.__name__} without passing `layer_idx` is not recommended and will "
				"to errors during the forward call, if caching is used. Please make sure to provide a `layer_idx` "
				"when creating this class."
			)

		self.attention_dropout = config.attention_dropout
		self.mulx=mulx
		self.hidden_size = config.hidden_size*mulx
		self.num_heads = config.num_attention_heads

		self.max_position_embeddings = config.max_position_embeddings
		self.rope_theta = config.rope_theta
		self.q_lora_rank = config.q_lora_rank*mulx
		self.qk_rope_head_dim = config.qk_rope_head_dim
		self.kv_lora_rank = config.kv_lora_rank*mulx
		self.v_head_dim = config.v_head_dim*mulx
		self.qk_nope_head_dim = config.qk_nope_head_dim*mulx
		self.q_head_dim = self.qk_nope_head_dim + self.qk_rope_head_dim

		self.is_causal = True

		if self.q_lora_rank is None:
			self.q_proj = nn.Linear(
				self.hidden_size, self.num_heads * self.q_head_dim, bias=False
			)
		else:
			self.q_a_proj = nn.Linear(
				self.hidden_size, self.q_lora_rank, bias=config.attention_bias
			)
			self.q_a_layernorm = DeepseekV2RMSNorm(self.q_lora_rank)
			self.q_b_proj = nn.Linear(
				self.q_lora_rank, self.num_heads * self.q_head_dim, bias=False
			)

		self.kv_a_proj_with_mqa = nn.Linear(
			self.hidden_size,
			self.kv_lora_rank + self.qk_rope_head_dim,
			bias=config.attention_bias,
		)
		self.kv_a_layernorm = DeepseekV2RMSNorm(self.kv_lora_rank)
		self.kv_b_proj = nn.Linear(
			self.kv_lora_rank,
			self.num_heads
			* (self.q_head_dim - self.qk_rope_head_dim + self.v_head_dim),
			bias=False,
		)

		if self.layer_idx==0: self.hidden_size *= 4
		elif self.layer_idx==8: self.hidden_size *= 8
		self.o_proj = nn.Linear(
			self.num_heads * self.v_head_dim,
			self.hidden_size,
			bias=config.attention_bias,
		)
		self._init_rope()

		self.softmax_scale = self.q_head_dim ** (-0.5)
		if self.config.rope_scaling is not None:
			mscale_all_dim = self.config.rope_scaling.get("mscale_all_dim", 0)
			scaling_factor = self.config.rope_scaling["factor"]
			if mscale_all_dim:
				mscale = yarn_get_mscale(scaling_factor, mscale_all_dim)
				self.softmax_scale = self.softmax_scale * mscale * mscale

	def _init_rope(self):
		if self.config.rope_scaling is None:
			self.rotary_emb = DeepseekV2RotaryEmbedding(
				self.qk_rope_head_dim,
				max_position_embeddings=self.max_position_embeddings,
				base=self.rope_theta,
			)
		else:
			scaling_type = self.config.rope_scaling["type"]
			scaling_factor = self.config.rope_scaling["factor"]
			if scaling_type == "linear":
				self.rotary_emb = DeepseekV2LinearScalingRotaryEmbedding(
					self.qk_rope_head_dim,
					max_position_embeddings=self.max_position_embeddings,
					scaling_factor=scaling_factor,
					base=self.rope_theta,
				)
			elif scaling_type == "dynamic":
				self.rotary_emb = DeepseekV2DynamicNTKScalingRotaryEmbedding(
					self.qk_rope_head_dim,
					max_position_embeddings=self.max_position_embeddings,
					scaling_factor=scaling_factor,
					base=self.rope_theta,
				)
			elif scaling_type == "yarn":
				kwargs = {
					key: self.config.rope_scaling[key]
					for key in [
						"original_max_position_embeddings",
						"beta_fast",
						"beta_slow",
						"mscale",
						"mscale_all_dim",
					]
					if key in self.config.rope_scaling
				}
				self.rotary_emb = DeepseekV2YarnRotaryEmbedding(
					self.qk_rope_head_dim,
					max_position_embeddings=self.max_position_embeddings,
					scaling_factor=scaling_factor,
					base=self.rope_theta,
					**kwargs,
				)
			else:
				raise ValueError(f"Unknown RoPE scaling type {scaling_type}")

	def _shape(self, tensor: torch.Tensor, seq_len: int, bsz: int):
		return (
			tensor.view(bsz, seq_len, self.num_heads, self.v_head_dim)
			.transpose(1, 2)
			.contiguous()
		)

	def forward(
		self,
		hidden_states: torch.Tensor,
		attention_mask: Optional[torch.Tensor] = None,
		position_ids: Optional[torch.LongTensor] = None,
		past_key_value: Optional[Cache] = None,
		output_attentions: bool = False,
		use_cache: bool = False,
		**kwargs,
	) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
		if "padding_mask" in kwargs:
			warnings.warn(
				"Passing `padding_mask` is deprecated and will be removed in v4.37. Please make sure use `attention_mask` instead.`"
			)
		bsz, q_len, _ = hidden_states.size()

		if self.q_lora_rank is None:
			q = self.q_proj(hidden_states)
		else:
			q = self.q_b_proj(self.q_a_layernorm(self.q_a_proj(hidden_states)))
		q = q.view(bsz, q_len, self.num_heads, self.q_head_dim).transpose(1, 2)
		q_nope, q_pe = torch.split(
			q, [self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1
		)

		compressed_kv = self.kv_a_proj_with_mqa(hidden_states)
		compressed_kv, k_pe = torch.split(
			compressed_kv, [self.kv_lora_rank, self.qk_rope_head_dim], dim=-1
		)
		k_pe = k_pe.view(bsz, q_len, 1, self.qk_rope_head_dim).transpose(1, 2)
		kv = (
			self.kv_b_proj(self.kv_a_layernorm(compressed_kv))
			.view(bsz, q_len, self.num_heads, self.qk_nope_head_dim + self.v_head_dim)
			.transpose(1, 2)
		)

		k_nope, value_states = torch.split(
			kv, [self.qk_nope_head_dim, self.v_head_dim], dim=-1
		)
		kv_seq_len = value_states.shape[-2]
		if past_key_value is not None:
			if self.layer_idx is None:
				raise ValueError(
					f"The cache structure has changed since version v4.36. If you are using {self.__class__.__name__} "
					"for auto-regressive decoding with k/v caching, please make sure to initialize the attention class "
					"with a layer index."
				)
			kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)
		cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)

		q_pe, k_pe = apply_rotary_pos_emb(q_pe, k_pe, cos, sin, position_ids)

		query_states = k_pe.new_empty(bsz, self.num_heads, q_len, self.q_head_dim)
		query_states[:, :, :, : self.qk_nope_head_dim] = q_nope
		query_states[:, :, :, self.qk_nope_head_dim :] = q_pe

		key_states = k_pe.new_empty(bsz, self.num_heads, q_len, self.q_head_dim)
		key_states[:, :, :, : self.qk_nope_head_dim] = k_nope
		key_states[:, :, :, self.qk_nope_head_dim :] = k_pe
		if past_key_value is not None:
			cache_kwargs = {"sin": sin, "cos": cos}	 # Specific to RoPE models
			key_states, value_states = past_key_value.update(
				key_states, value_states, self.layer_idx, cache_kwargs
			)
		
		if self.layer_idx==0:
			for item in {query_states, key_states, value_states}:
				tensor_add = torch.zeros((bsz, self.num_heads,3,item.shape[-1]))
				item = torch.cat([item, tensor_add], dim=-2)
				item = torch.cat([item[:, :, :-3, :], item[:, :, 1:-2, :], item[:, :, 2:-1, :], item[:, :, 3:, :]], dim=-1)
		elif self.layer_idx==8:
			for item in {query_states, key_states, value_states}:
				tensor_add = torch.zeros((bsz, self.num_heads,7,item.shape[-1]))
				item = torch.cat([item, tensor_add], dim=-2)
				item = torch.cat([item[:, :-7, :], item[:, 1:-6, :], item[:, 2:-5, :], item[:, 3:-4, :],item[:, 4:-3, :], item[:, 5:-2, :], item[:, 6:-1, :], item[:, 7:, :]], dim=-1)

		attn_weights = (
			torch.matmul(query_states, key_states.transpose(2, 3)) * self.softmax_scale
		)

		if attn_weights.size() != (bsz, self.num_heads, q_len, kv_seq_len):
			raise ValueError(
				f"Attention weights should be of size {(bsz, self.num_heads, q_len, kv_seq_len)}, but is"
				f" {attn_weights.size()}"
			)
		assert attention_mask is not None
		if attention_mask is not None:
			if attention_mask.size() != (bsz, 1, q_len, kv_seq_len):
				raise ValueError(
					f"Attention mask should be of size {(bsz, 1, q_len, kv_seq_len)}, but is {attention_mask.size()}"
				)
			attn_weights = attn_weights + attention_mask

		# upcast attention to fp32
		attn_weights = nn.functional.softmax(
			attn_weights, dim=-1, dtype=torch.float32
		).to(query_states.dtype)
		attn_weights = nn.functional.dropout(
			attn_weights, p=self.attention_dropout, training=self.training
		)
		attn_output = torch.matmul(attn_weights, value_states)

		if attn_output.size() != (bsz, self.num_heads, q_len, self.v_head_dim):
			raise ValueError(
				f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.v_head_dim)}, but is"
				f" {attn_output.size()}"
			)

		attn_output = attn_output.transpose(1, 2).contiguous()

		attn_output = attn_output.reshape(bsz, q_len, self.num_heads * self.v_head_dim)

		attn_output = self.o_proj(attn_output)

		if not output_attentions:
			attn_weights = None

		return attn_output, attn_weights, past_key_value


# Copied from transformers.models.llama.modeling_llama.LlamaFlashAttention2 with Llama->DeepseekV2
class DeepseekV2FlashAttention2(DeepseekV2Attention):
	"""
	DeepseekV2 flash attention module. This module inherits from `DeepseekV2Attention` as the weights of the module stays
	untouched. The only required change would be on the forward pass where it needs to correctly call the public API of
	flash attention and deal with padding tokens in case the input contains any of them.
	"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		# TODO: Should be removed once Flash Attention for RoCm is bumped to 2.1.
		# flash_attn<2.1 generates top-left aligned causal mask, while what is needed here is bottom-right alignement, that was made default for flash_attn>=2.1. This attribute is used to handle this difference. Reference: https://github.com/Dao-AILab/flash-attention/releases/tag/v2.1.0.
		# Beware that with flash_attn<2.1, using q_seqlen != k_seqlen (except for the case q_seqlen == 1) produces a wrong mask (top-left).
		self._flash_attn_uses_top_left_mask = not is_flash_attn_greater_or_equal_2_10()

	def forward(
		self,
		hidden_states: torch.Tensor,
		attention_mask: Optional[torch.LongTensor] = None,
		position_ids: Optional[torch.LongTensor] = None,
		past_key_value: Optional[Cache] = None,
		output_attentions: bool = False,
		use_cache: bool = False,
		**kwargs,
	) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
		# DeepseekV2FlashAttention2 attention does not support output_attentions
		if "padding_mask" in kwargs:
			warnings.warn(
				"Passing `padding_mask` is deprecated and will be removed in v4.37. Please make sure use `attention_mask` instead.`"
			)

			# overwrite attention_mask with padding_mask
			attention_mask = kwargs.pop("padding_mask")

		output_attentions = False

		bsz, q_len, _ = hidden_states.size()

		if self.q_lora_rank is None:
			q = self.q_proj(hidden_states)
		else:
			q = self.q_b_proj(self.q_a_layernorm(self.q_a_proj(hidden_states)))
		q = q.view(bsz, q_len, self.num_heads, self.q_head_dim).transpose(1, 2)
		q_nope, q_pe = torch.split(
			q, [self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1
		)

		# Flash attention requires the input to have the shape
		# batch_size x seq_length x head_dim x hidden_dim
		# therefore we just need to keep the original shape
		compressed_kv = self.kv_a_proj_with_mqa(hidden_states)
		compressed_kv, k_pe = torch.split(
			compressed_kv, [self.kv_lora_rank, self.qk_rope_head_dim], dim=-1
		)
		k_pe = k_pe.view(bsz, q_len, 1, self.qk_rope_head_dim).transpose(1, 2)
		kv = (
			self.kv_b_proj(self.kv_a_layernorm(compressed_kv))
			.view(bsz, q_len, self.num_heads, self.qk_nope_head_dim + self.v_head_dim)
			.transpose(1, 2)
		)

		k_nope, value_states = torch.split(
			kv, [self.qk_nope_head_dim, self.v_head_dim], dim=-1
		)
		kv_seq_len = value_states.shape[-2]

		kv_seq_len = value_states.shape[-2]
		if past_key_value is not None:
			kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)

		cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
		q_pe, k_pe = apply_rotary_pos_emb(q_pe, k_pe, cos, sin, position_ids)

		query_states = k_pe.new_empty(bsz, self.num_heads, q_len, self.q_head_dim)
		query_states[:, :, :, : self.qk_nope_head_dim] = q_nope
		query_states[:, :, :, self.qk_nope_head_dim :] = q_pe

		key_states = k_pe.new_empty(bsz, self.num_heads, q_len, self.q_head_dim)
		key_states[:, :, :, : self.qk_nope_head_dim] = k_nope
		key_states[:, :, :, self.qk_nope_head_dim :] = k_pe

		if self.q_head_dim != self.v_head_dim:
			value_states = F.pad(value_states, [0, self.q_head_dim - self.v_head_dim])

		if past_key_value is not None:
			cache_kwargs = {"sin": sin, "cos": cos}	 # Specific to RoPE models
			key_states, value_states = past_key_value.update(
				key_states, value_states, self.layer_idx, cache_kwargs
			)

		# TODO: These transpose are quite inefficient but Flash Attention requires the layout [batch_size, sequence_length, num_heads, head_dim]. We would need to refactor the KV cache
		# to be able to avoid many of these transpose/reshape/view.
		query_states = query_states.transpose(1, 2)
		key_states = key_states.transpose(1, 2)
		value_states = value_states.transpose(1, 2)

		dropout_rate = self.attention_dropout if self.training else 0.0

		# In PEFT, usually we cast the layer norms in float32 for training stability reasons
		# therefore the input hidden states gets silently casted in float32. Hence, we need
		# cast them back in the correct dtype just to be sure everything works as expected.
		# This might slowdown training & inference so it is recommended to not cast the LayerNorms
		# in fp32. (DeepseekV2RMSNorm handles it correctly)

		input_dtype = query_states.dtype
		if input_dtype == torch.float32:
			# Handle the case where the model is quantized
			if hasattr(self.config, "_pre_quantization_dtype"):
				target_dtype = self.config._pre_quantization_dtype
			elif torch.is_autocast_enabled():
				target_dtype = torch.get_autocast_gpu_dtype()
			else:
				target_dtype = (
					self.q_proj.weight.dtype
					if self.q_lora_rank is None
					else self.q_a_proj.weight.dtype
				)

			logger.warning_once(
				f"The input hidden states seems to be silently casted in float32, this might be related to"
				f" the fact you have upcasted embedding or layer norm layers in float32. We will cast back the input in"
				f" {target_dtype}."
			)

			query_states = query_states.to(target_dtype)
			key_states = key_states.to(target_dtype)
			value_states = value_states.to(target_dtype)

		attn_output = self._flash_attention_forward(
			query_states,
			key_states,
			value_states,
			attention_mask,
			q_len,
			dropout=dropout_rate,
			softmax_scale=self.softmax_scale,
		)
		if self.q_head_dim != self.v_head_dim:
			attn_output = attn_output[:, :, :, : self.v_head_dim]

		attn_output = attn_output.reshape(
			bsz, q_len, self.num_heads * self.v_head_dim
		).contiguous()
		attn_output = self.o_proj(attn_output)

		if not output_attentions:
			attn_weights = None

		return attn_output, attn_weights, past_key_value

	def _flash_attention_forward(
		self,
		query_states,
		key_states,
		value_states,
		attention_mask,
		query_length,
		dropout=0.0,
		softmax_scale=None,
	):
		"""
		Calls the forward method of Flash Attention - if the input hidden states contain at least one padding token
		first unpad the input, then computes the attention scores and pad the final attention scores.

		Args:
			query_states (`torch.Tensor`):
				Input query states to be passed to Flash Attention API
			key_states (`torch.Tensor`):
				Input key states to be passed to Flash Attention API
			value_states (`torch.Tensor`):
				Input value states to be passed to Flash Attention API
			attention_mask (`torch.Tensor`):
				The padding mask - corresponds to a tensor of size `(batch_size, seq_len)` where 0 stands for the
				position of padding tokens and 1 for the position of non-padding tokens.
			dropout (`int`, *optional*):
				Attention dropout
			softmax_scale (`float`, *optional*):
				The scaling of QK^T before applying softmax. Default to 1 / sqrt(head_dim)
		"""
		if not self._flash_attn_uses_top_left_mask:
			causal = self.is_causal
		else:
			# TODO: Remove the `query_length != 1` check once Flash Attention for RoCm is bumped to 2.1. For details, please see the comment in DeepseekV2FlashAttention2 __init__.
			causal = self.is_causal and query_length != 1

		# Contains at least one padding token in the sequence
		if attention_mask is not None:
			batch_size = query_states.shape[0]
			(
				query_states,
				key_states,
				value_states,
				indices_q,
				cu_seq_lens,
				max_seq_lens,
			) = self._upad_input(
				query_states, key_states, value_states, attention_mask, query_length
			)

			cu_seqlens_q, cu_seqlens_k = cu_seq_lens
			max_seqlen_in_batch_q, max_seqlen_in_batch_k = max_seq_lens

			attn_output_unpad = flash_attn_varlen_func(
				query_states,
				key_states,
				value_states,
				cu_seqlens_q=cu_seqlens_q,
				cu_seqlens_k=cu_seqlens_k,
				max_seqlen_q=max_seqlen_in_batch_q,
				max_seqlen_k=max_seqlen_in_batch_k,
				dropout_p=dropout,
				softmax_scale=softmax_scale,
				causal=causal,
			)

			attn_output = pad_input(
				attn_output_unpad, indices_q, batch_size, query_length
			)
		else:
			attn_output = flash_attn_func(
				query_states,
				key_states,
				value_states,
				dropout,
				softmax_scale=softmax_scale,
				causal=causal,
			)

		return attn_output

	def _upad_input(
		self, query_layer, key_layer, value_layer, attention_mask, query_length
	):
		indices_k, cu_seqlens_k, max_seqlen_in_batch_k = _get_unpad_data(attention_mask)
		batch_size, kv_seq_len, num_key_value_heads, head_dim = key_layer.shape

		key_layer = index_first_axis(
			key_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim),
			indices_k,
		)
		value_layer = index_first_axis(
			value_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim),
			indices_k,
		)
		if query_length == kv_seq_len:
			query_layer = index_first_axis(
				query_layer.reshape(batch_size * kv_seq_len, self.num_heads, head_dim),
				indices_k,
			)
			cu_seqlens_q = cu_seqlens_k
			max_seqlen_in_batch_q = max_seqlen_in_batch_k
			indices_q = indices_k
		elif query_length == 1:
			max_seqlen_in_batch_q = 1
			cu_seqlens_q = torch.arange(
				batch_size + 1, dtype=torch.int32, device=query_layer.device
			)  # There is a memcpy here, that is very bad.
			indices_q = cu_seqlens_q[:-1]
			query_layer = query_layer.squeeze(1)
		else:
			# The -q_len: slice assumes left padding.
			attention_mask = attention_mask[:, -query_length:]
			query_layer, indices_q, cu_seqlens_q, max_seqlen_in_batch_q = unpad_input(
				query_layer, attention_mask
			)

		return (
			query_layer,
			key_layer,
			value_layer,
			indices_q,
			(cu_seqlens_q, cu_seqlens_k),
			(max_seqlen_in_batch_q, max_seqlen_in_batch_k),
		)


ATTENTION_CLASSES = {
	"eager": DeepseekV2Attention,
	"flash_attention_2": DeepseekV2FlashAttention2,
}


class DeepseekV2DecoderLayer(nn.Module):
	def __init__(self, config: DeepseekV2Config, layer_idx: int):
		super().__init__()
		self.layer_idx = layer_idx
		self.hidden_size = config.hidden_size
		mulx = sizelist[layer_idx]
		size = mulx*xsize
		self.self_attn = ATTENTION_CLASSES[config._attn_implementation](
			config=config, layer_idx=layer_idx, mulx=mulx
		)
		self.input_layernorm = DeepseekV2RMSNorm(
			size, eps=config.rms_norm_eps
		)
		
		if self.layer_idx==0: size *=4
		elif self.layer_idx==8: size *=8
		self.mlp = (
			DeepseekV2MoE(config, size)
			if (
				config.n_routed_experts is not None
				and layer_idx >= config.first_k_dense_replace
				and layer_idx % config.moe_layer_freq == 0
			)
			else DeepseekV2MLP(config, size, size)
		)

		self.post_attention_layernorm = DeepseekV2RMSNorm(
			size, eps=config.rms_norm_eps
		)

	def forward(
		self,
		hidden_states: torch.Tensor,
		attention_mask: Optional[torch.Tensor] = None,
		position_ids: Optional[torch.LongTensor] = None,
		past_key_value: Optional[Tuple[torch.Tensor]] = None,
		output_attentions: Optional[bool] = False,
		use_cache: Optional[bool] = False,
		**kwargs,
	) -> Tuple[
		torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]
	]:
		"""
		Args:
			hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
			attention_mask (`torch.FloatTensor`, *optional*):
				attention mask of size `(batch_size, sequence_length)` if flash attention is used or `(batch_size, 1,
				query_sequence_length, key_sequence_length)` if default attention is used.
			output_attentions (`bool`, *optional*):
				Whether or not to return the attentions tensors of all attention layers. See `attentions` under
				returned tensors for more detail.
			use_cache (`bool`, *optional*):
				If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
				(see `past_key_values`).
			past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
		"""
		if "padding_mask" in kwargs:
			warnings.warn(
				"Passing `padding_mask` is deprecated and will be removed in v4.37. Please make sure use `attention_mask` instead.`"
			)
			
		residual = hidden_states
		if self.layer_idx==0: 
			tensor_add = torch.zeros((residual.shape[0],3,xsize))
			#print(residual.shape,tensor_add.shape)
			residual = torch.cat([residual, tensor_add], dim=-2)
			residual = torch.cat([residual[:, :-3, :], residual[:, 1:-2, :], residual[:, 2:-1, :], residual[:, 3:, :]], dim=-1)
		elif self.layer_idx==8: 
			tensor_add = torch.zeros((residual.shape[0],7,xsize))
			#print(residual.shape,tensor_add.shape)
			residual = torch.cat([residual, tensor_add], dim=-2)
			residual = torch.cat([residual[:, :-7, :], residual[:, 1:-6, :], residual[:, 2:-5, :], residual[:, 3:-4, :],residual[:, 4:-3, :], residual[:, 5:-2, :], residual[:, 6:-1, :], residual[:, 7:, :]], dim=-1)

		hidden_states = self.input_layernorm(hidden_states)

		# Self Attention
		hidden_states, self_attn_weights, present_key_value = self.self_attn(
			hidden_states=hidden_states,
			attention_mask=attention_mask,
			position_ids=position_ids,
			past_key_value=past_key_value,
			output_attentions=output_attentions,
			use_cache=use_cache,
			**kwargs,
		)
		
		hidden_states = residual + hidden_states

		# Fully Connected
		residual = hidden_states
		hidden_states = self.post_attention_layernorm(hidden_states)
		hidden_states = self.mlp(hidden_states)
		hidden_states = residual + hidden_states

		outputs = (hidden_states,)

		if output_attentions:
			outputs += (self_attn_weights,)

		if use_cache:
			outputs += (present_key_value,)

		return outputs


DeepseekV2_START_DOCSTRING = r"""
	This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
	library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
	etc.)

	This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
	Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
	and behavior.

	Parameters:
		config ([`DeepseekV2Config`]):
			Model configuration class with all the parameters of the model. Initializing with a config file does not
			load the weights associated with the model, only the configuration. Check out the
			[`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""


@add_start_docstrings(
	"The bare DeepseekV2 Model outputting raw hidden-states without any specific head on top.",
	DeepseekV2_START_DOCSTRING,
)
class DeepseekV2PreTrainedModel(PreTrainedModel):
	config_class = DeepseekV2Config
	base_model_prefix = "model"
	supports_gradient_checkpointing = True
	_no_split_modules = ["DeepseekV2DecoderLayer"]
	_skip_keys_device_placement = "past_key_values"
	_supports_flash_attn_2 = True
	_supports_cache_class = True

	def _init_weights(self, module):
		'''std = self.config.initializer_range
		if isinstance(module, nn.Linear):
			module.weight.data.normal_(mean=0.0, std=std)
			if module.bias is not None:
				module.bias.data.zero_()
		elif isinstance(module, nn.Embedding):
			module.weight.data.normal_(mean=0.0, std=std)
			if module.padding_idx is not None:
				module.weight.data[module.padding_idx].zero_()'''


DeepseekV2_INPUTS_DOCSTRING = r"""
	Args:
		input_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`):
			Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you provide
			it.

			Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
			[`PreTrainedTokenizer.__call__`] for details.

			[What are input IDs?](../glossary#input-ids)
		attention_mask (`torch.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
			Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

			- 1 for tokens that are **not masked**,
			- 0 for tokens that are **masked**.

			[What are attention masks?](../glossary#attention-mask)

			Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
			[`PreTrainedTokenizer.__call__`] for details.

			If `past_key_values` is used, optionally only the last `input_ids` have to be input (see
			`past_key_values`).

			If you want to change padding behavior, you should read [`modeling_opt._prepare_decoder_attention_mask`]
			and modify to your needs. See diagram 1 in [the paper](https://arxiv.org/abs/1910.13461) for more
			information on the default strategy.

			- 1 indicates the head is **not masked**,
			- 0 indicates the head is **masked**.
		position_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
			Indices of positions of each input sequence tokens in the position embeddings. Selected in the range `[0,
			config.n_positions - 1]`.

			[What are position IDs?](../glossary#position-ids)
		past_key_values (`Cache` or `tuple(tuple(torch.FloatTensor))`, *optional*):
			Pre-computed hidden-states (key and values in the self-attention blocks and in the cross-attention
			blocks) that can be used to speed up sequential decoding. This typically consists in the `past_key_values`
			returned by the model at a previous stage of decoding, when `use_cache=True` or `config.use_cache=True`.

			Two formats are allowed:
			- a [`~cache_utils.Cache`] instance;
			- Tuple of `tuple(torch.FloatTensor)` of length `config.n_layers`, with each tuple having 2 tensors of
			shape `(batch_size, num_heads, sequence_length, embed_size_per_head)`). This is also known as the legacy
			cache format.

			The model will output the same cache format that is fed as input. If no `past_key_values` are passed, the
			legacy cache format will be returned.

			If `past_key_values` are used, the user can optionally input only the last `input_ids` (those that don't
			have their past key value states given to this model) of shape `(batch_size, 1)` instead of all `input_ids`
			of shape `(batch_size, sequence_length)`.
		inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
			Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation. This
			is useful if you want more control over how to convert `input_ids` indices into associated vectors than the
			model's internal embedding lookup matrix.
		use_cache (`bool`, *optional*):
			If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
			`past_key_values`).
		output_attentions (`bool`, *optional*):
			Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
			tensors for more detail.
		output_hidden_states (`bool`, *optional*):
			Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
			more detail.
		return_dict (`bool`, *optional*):
			Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
"""


@add_start_docstrings(
	"The bare DeepseekV2 Model outputting raw hidden-states without any specific head on top.",
	DeepseekV2_START_DOCSTRING,
)
class DeepseekV2Model(DeepseekV2PreTrainedModel):
	"""
	Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`DeepseekV2DecoderLayer`]

	Args:
		config: DeepseekV2Config
	"""

	def __init__(self, config: DeepseekV2Config):
		super().__init__(config)
		self.padding_idx = config.pad_token_id
		self.vocab_size = config.vocab_size

		self.embed_tokens = nn.Embedding(
			config.vocab_size, config.hidden_size, self.padding_idx
		)
		self.layers = nn.ModuleList(
			[
				DeepseekV2DecoderLayer(config, layer_idx)
				for layer_idx in range(config.num_hidden_layers)
			]
		)
		self._use_flash_attention_2 = config._attn_implementation == "flash_attention_2"
		self.norm = DeepseekV2RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

		self.gradient_checkpointing = False
		self.dense_2h_to_h = nn.Linear(config.hidden_size*2, config.hidden_size)
		self.dense_4h_to_2h = nn.Linear(config.hidden_size*4, config.hidden_size*2)
		self.dense_8h_to_4h = nn.Linear(config.hidden_size*8, config.hidden_size*4)
		# Initialize weights and apply final processing
		self.post_init()

	def get_input_embeddings(self):
		return self.embed_tokens

	def set_input_embeddings(self, value):
		self.embed_tokens = value

	@add_start_docstrings_to_model_forward(DeepseekV2_INPUTS_DOCSTRING)
	def forward(
		self,
		input_ids: torch.LongTensor = None,
		attention_mask: Optional[torch.Tensor] = None,
		position_ids: Optional[torch.LongTensor] = None,
		past_key_values: Optional[List[torch.FloatTensor]] = None,
		inputs_embeds: Optional[torch.FloatTensor] = None,
		use_cache: Optional[bool] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		return_dict: Optional[bool] = None,
	) -> Union[Tuple, BaseModelOutputWithPast]:
		output_attentions = (
			output_attentions
			if output_attentions is not None
			else self.config.output_attentions
		)
		output_hidden_states = (
			output_hidden_states
			if output_hidden_states is not None
			else self.config.output_hidden_states
		)
		use_cache = use_cache if use_cache is not None else self.config.use_cache

		return_dict = (
			return_dict if return_dict is not None else self.config.return_dict
		)

		# retrieve input_ids and inputs_embeds
		if input_ids is not None and inputs_embeds is not None:
			raise ValueError(
				"You cannot specify both input_ids and inputs_embeds at the same time"
			)
		elif input_ids is not None:
			batch_size, seq_length = input_ids.shape[:2]
		elif inputs_embeds is not None:
			batch_size, seq_length = inputs_embeds.shape[:2]
		else:
			raise ValueError("You have to specify either input_ids or inputs_embeds")

		if self.gradient_checkpointing and self.training:
			if use_cache:
				logger.warning_once(
					"`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`transformers."
				)
				use_cache = False

		if inputs_embeds is None:
			inputs_embeds = self.embed_tokens(input_ids)

		# embed positions
		#tensor_add7 = torch.zeros((1,7,xsize))
		#inputs_embeds = torch.cat([inputs_embeds, tensor_add7], dim=-2)
		hidden_states = inputs_embeds
		#hidden_states4 = torch.cat([hidden_states[:, :-7, :], hidden_states[:, 1:-6, :], hidden_states[:, 2:-5, :], hidden_states[:, 3:-4, :]], dim=-1)
		#inputs_embeds = hidden_states4
		#hidden_states8 = torch.cat([hidden_states[:, :-7, :], hidden_states[:, 1:-6, :], hidden_states[:, 2:-5, :], hidden_states[:, 3:-4, :],hidden_states[:, 4:-3, :], hidden_states[:, 5:-2, :], hidden_states[:, 6:-1, :], hidden_states[:, 7:, :]], dim=-1) 
		
		seq_length = hidden_states.shape[1]
		past_key_values_length = 0
		if use_cache:
			use_legacy_cache = not isinstance(past_key_values, Cache)
			if use_legacy_cache:
				past_key_values = DynamicCache.from_legacy_cache(past_key_values)
			past_key_values_length = past_key_values.get_usable_length(seq_length)

		if position_ids is None:
			device = input_ids.device if input_ids is not None else inputs_embeds.device
			position_ids = torch.arange(
				past_key_values_length,
				seq_length + past_key_values_length,
				dtype=torch.long,
				device=device,
			)
			position_ids = position_ids.unsqueeze(0)
			#print(seq_length,past_key_values_length,position_ids)
		#attention_mask=attention_mask[:,:-3]	
		if self._use_flash_attention_2:
			# 2d mask is passed through the layers
			attention_mask = (
				attention_mask
				if (attention_mask is not None and 0 in attention_mask)
				else None
			)
		else:
			# 4d mask is passed through the layers
			attention_mask = _prepare_4d_causal_attention_mask(
				attention_mask,
				(batch_size, seq_length),
				inputs_embeds,
				past_key_values_length,
			)

		# decoder layers
		all_hidden_states = () if output_hidden_states else None
		all_self_attns = () if output_attentions else None
		next_decoder_cache = None

		j = 0
		#channel==4
		for i in range(2):
			#print(j)
			if self.gradient_checkpointing and self.training:
				layer_outputs = self._gradient_checkpointing_func(
					self.layers[j].__call__,
					hidden_states,
					attention_mask,
					position_ids,
					past_key_values,
					output_attentions,
					use_cache,
				)
			else:
				layer_outputs = self.layers[j](
					hidden_states,
					attention_mask=attention_mask,
					position_ids=position_ids,
					past_key_value=past_key_values,
					output_attentions=output_attentions,
					use_cache=use_cache,
				)
			hidden_states = layer_outputs[0]
			if use_cache:
				next_decoder_cache = layer_outputs[2 if output_attentions else 1]

			if output_attentions:
				all_self_attns += (layer_outputs[1],)
			j+=1
		hidden_states = self.dense_4h_to_2h(hidden_states)
		for i in range(3):
			#print(j)
			if self.gradient_checkpointing and self.training:
				layer_outputs = self._gradient_checkpointing_func(
					self.layers[j].__call__,
					hidden_states,
					attention_mask,
					position_ids,
					past_key_values,
					output_attentions,
					use_cache,
				)
			else:
				layer_outputs = self.layers[j](
					hidden_states,
					attention_mask=attention_mask,
					position_ids=position_ids,
					past_key_value=past_key_values,
					output_attentions=output_attentions,
					use_cache=use_cache,
				)
			hidden_states = layer_outputs[0]
			if use_cache:
				next_decoder_cache = layer_outputs[2 if output_attentions else 1]

			if output_attentions:
				all_self_attns += (layer_outputs[1],)
			j+=1
		hidden_states = self.dense_2h_to_h(hidden_states)
		for i in range(3):
			#print(j)
			if self.gradient_checkpointing and self.training:
				layer_outputs = self._gradient_checkpointing_func(
					self.layers[j].__call__,
					hidden_states,
					attention_mask,
					position_ids,
					past_key_values,
					output_attentions,
					use_cache,
				)
			else:
				layer_outputs = self.layers[j](
					hidden_states,
					attention_mask=attention_mask,
					position_ids=position_ids,
					past_key_value=past_key_values,
					output_attentions=output_attentions,
					use_cache=use_cache,
				)
			hidden_states = layer_outputs[0]
			if use_cache:
				next_decoder_cache = layer_outputs[2 if output_attentions else 1]

			if output_attentions:
				all_self_attns += (layer_outputs[1],)
			j+=1
		#channel==8
		#print(j)
		if self.gradient_checkpointing and self.training:
			layer_outputs = self._gradient_checkpointing_func(
				self.layers[j].__call__,
				inputs_embeds,
				attention_mask,
				position_ids,
				past_key_values,
				output_attentions,
				use_cache,
			)
		else:
			layer_outputs = self.layers[j](
				inputs_embeds,
				attention_mask=attention_mask,
				position_ids=position_ids,
				past_key_value=past_key_values,
				output_attentions=output_attentions,
				use_cache=use_cache,
			)
		hidden_states8 = layer_outputs[0]
		if use_cache:
			next_decoder_cache = layer_outputs[2 if output_attentions else 1]

		if output_attentions:
			all_self_attns += (layer_outputs[1],)
		j+=1
		
		hidden_states8 = self.dense_8h_to_4h(hidden_states8)
		#print(j)
		if self.gradient_checkpointing and self.training:
			layer_outputs = self._gradient_checkpointing_func(
				self.layers[j].__call__,
				hidden_states8,
				attention_mask,
				position_ids,
				past_key_values,
				output_attentions,
				use_cache,
			)
		else:
			layer_outputs = self.layers[j](
				hidden_states8,
				attention_mask=attention_mask,
				position_ids=position_ids,
				past_key_value=past_key_values,
				output_attentions=output_attentions,
				use_cache=use_cache,
			)
		hidden_states8 = layer_outputs[0]
		if use_cache:
			next_decoder_cache = layer_outputs[2 if output_attentions else 1]
		if output_attentions:
			all_self_attns += (layer_outputs[1],)
		j+=1
		
		hidden_states8 = self.dense_4h_to_2h(hidden_states8)
		for i in range(2):
			#print(j)
			if self.gradient_checkpointing and self.training:
				layer_outputs = self._gradient_checkpointing_func(
					self.layers[j].__call__,
					hidden_states8,
					attention_mask,
					position_ids,
					past_key_values,
					output_attentions,
					use_cache,
				)
			else:
				layer_outputs = self.layers[j](
					hidden_states8,
					attention_mask=attention_mask,
					position_ids=position_ids,
					past_key_value=past_key_values,
					output_attentions=output_attentions,
					use_cache=use_cache,
				)
			hidden_states8 = layer_outputs[0]
			if use_cache:
				next_decoder_cache = layer_outputs[2 if output_attentions else 1]

			if output_attentions:
				all_self_attns += (layer_outputs[1],)
			j+=1
		hidden_states8 = self.dense_2h_to_h(hidden_states8) 
		for i in range(2):
			#print(j)
			if self.gradient_checkpointing and self.training:
				layer_outputs = self._gradient_checkpointing_func(
					self.layers[j].__call__,
					hidden_states8,
					attention_mask,
					position_ids,
					past_key_values,
					output_attentions,
					use_cache,
				)
			else:
				layer_outputs = self.layers[j](
					hidden_states8,
					attention_mask=attention_mask,
					position_ids=position_ids,
					past_key_value=past_key_values,
					output_attentions=output_attentions,
					use_cache=use_cache,
				)
			hidden_states8 = layer_outputs[0]
			if use_cache:
				next_decoder_cache = layer_outputs[2 if output_attentions else 1]

			if output_attentions:
				all_self_attns += (layer_outputs[1],)
			j+=1

		hidden_states = torch.cat([hidden_states, hidden_states8], dim=-1)
		hidden_states = self.dense_2h_to_h(hidden_states)
		
		'''
		for decoder_layer in self.layers:
			if output_hidden_states:
				all_hidden_states += (hidden_states,)

			if self.gradient_checkpointing and self.training:
				layer_outputs = self._gradient_checkpointing_func(
					decoder_layer.__call__,
					hidden_states,
					attention_mask,
					position_ids,
					past_key_values,
					output_attentions,
					use_cache,
				)
			else:
				layer_outputs = decoder_layer(
					hidden_states,
					attention_mask=attention_mask,
					position_ids=position_ids,
					past_key_value=past_key_values,
					output_attentions=output_attentions,
					use_cache=use_cache,
				)

			hidden_states = layer_outputs[0]

			if use_cache:
				next_decoder_cache = layer_outputs[2 if output_attentions else 1]

			if output_attentions:
				all_self_attns += (layer_outputs[1],)
		'''

		hidden_states = self.norm(hidden_states)

		# add hidden states from the last decoder layer
		if output_hidden_states:
			all_hidden_states += (hidden_states,)

		next_cache = None
		if use_cache:
			next_cache = (
				next_decoder_cache.to_legacy_cache()
				if use_legacy_cache
				else next_decoder_cache
			)
		if not return_dict:
			return tuple(
				v
				for v in [hidden_states, next_cache, all_hidden_states, all_self_attns]
				if v is not None
			)
		return BaseModelOutputWithPast(
			last_hidden_state=hidden_states,
			past_key_values=next_cache,
			hidden_states=all_hidden_states,
			attentions=all_self_attns,
		)


class DeepseekV2ForCausalLM(DeepseekV2PreTrainedModel, GenerationMixin):
	_tied_weights_keys = ["lm_head.weight"]

	def __init__(self, config):
		super().__init__(config)
		self.model = DeepseekV2Model(config)
		self.vocab_size = config.vocab_size
		self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

		# Initialize weights and apply final processing
		self.post_init()

	def get_input_embeddings(self):
		return self.model.embed_tokens

	def set_input_embeddings(self, value):
		self.model.embed_tokens = value

	def get_output_embeddings(self):
		return self.lm_head

	def set_output_embeddings(self, new_embeddings):
		self.lm_head = new_embeddings

	def set_decoder(self, decoder):
		self.model = decoder

	def get_decoder(self):
		return self.model

	@add_start_docstrings_to_model_forward(DeepseekV2_INPUTS_DOCSTRING)
	@replace_return_docstrings(
		output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC
	)
	def forward(
		self,
		input_ids: torch.LongTensor = None,
		attention_mask: Optional[torch.Tensor] = None,
		position_ids: Optional[torch.LongTensor] = None,
		past_key_values: Optional[List[torch.FloatTensor]] = None,
		inputs_embeds: Optional[torch.FloatTensor] = None,
		labels: Optional[torch.LongTensor] = None,
		use_cache: Optional[bool] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		return_dict: Optional[bool] = None,
	) -> Union[Tuple, CausalLMOutputWithPast]:
		r"""
		Args:
			labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
				Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
				config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
				(masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

		Returns:

		Example:

		```python
		>>> from transformers import AutoTokenizer, DeepseekV2ForCausalLM

		>>> model = DeepseekV2ForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
		>>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

		>>> prompt = "Hey, are you conscious? Can you talk to me?"
		>>> inputs = tokenizer(prompt, return_tensors="pt")

		>>> # Generate
		>>> generate_ids = model.generate(inputs.input_ids, max_length=30)
		>>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
		"Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
		```"""
		output_attentions = (
			output_attentions
			if output_attentions is not None
			else self.config.output_attentions
		)
		output_hidden_states = (
			output_hidden_states
			if output_hidden_states is not None
			else self.config.output_hidden_states
		)
		return_dict = (
			return_dict if return_dict is not None else self.config.return_dict
		)

		# decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
		outputs = self.model(
			input_ids=input_ids,
			attention_mask=attention_mask,
			position_ids=position_ids,
			past_key_values=past_key_values,
			inputs_embeds=inputs_embeds,
			use_cache=use_cache,
			output_attentions=output_attentions,
			output_hidden_states=output_hidden_states,
			return_dict=return_dict,
		)

		hidden_states = outputs[0]
		logits = self.lm_head(hidden_states)
		logits = logits.float()

		loss = None
		if labels is None:
			labels=input_ids
		# Shift so that tokens < n predict n
		shift_logits = logits[..., :-1, :].contiguous()
		shift_labels = labels[..., 1:].contiguous()
		# Flatten the tokens
		loss_fct = CrossEntropyLoss()
		shift_logits = shift_logits.view(-1, self.config.vocab_size)
		shift_labels = shift_labels.view(-1)
		# Enable model parallelism
		shift_labels = shift_labels.to(shift_logits.device)
		loss = loss_fct(shift_logits, shift_labels)

		if not return_dict:
			output = (logits,) + outputs[1:]
			return (loss,) + output if loss is not None else output

		return CausalLMOutputWithPast(
			loss=loss,
			logits=logits,
			past_key_values=outputs.past_key_values,
			hidden_states=outputs.hidden_states,
			attentions=outputs.attentions,
		)

	def prepare_inputs_for_generation(
		self,
		input_ids,
		past_key_values=None,
		attention_mask=None,
		inputs_embeds=None,
		**kwargs,
	):
		if past_key_values is not None:
			if isinstance(past_key_values, Cache):
				cache_length = past_key_values.get_seq_length()
				past_length = past_key_values.seen_tokens
				max_cache_length = past_key_values.get_max_length()
			else:
				cache_length = past_length = past_key_values[0][0].shape[2]
				max_cache_length = None

			# Keep only the unprocessed tokens:
			# 1 - If the length of the attention_mask exceeds the length of input_ids, then we are in a setting where
			# some of the inputs are exclusivelly passed as part of the cache (e.g. when passing input_embeds as
			# input)
			if (
				attention_mask is not None
				and attention_mask.shape[1] > input_ids.shape[1]
			):
				input_ids = input_ids[:, -(attention_mask.shape[1] - past_length) :]
			# 2 - If the past_length is smaller than input_ids', then input_ids holds all input tokens. We can discard
			# input_ids based on the past_length.
			elif past_length < input_ids.shape[1]:
				input_ids = input_ids[:, past_length:]
			# 3 - Otherwise (past_length >= input_ids.shape[1]), let's assume input_ids only has unprocessed tokens.

			# If we are about to go beyond the maximum cache length, we need to crop the input attention mask.
			if (
				max_cache_length is not None
				and attention_mask is not None
				and cache_length + input_ids.shape[1] > max_cache_length
			):
				attention_mask = attention_mask[:, -max_cache_length:]

		position_ids = kwargs.get("position_ids", None)
		if attention_mask is not None and position_ids is None:
			# create position_ids on the fly for batch generation
			position_ids = attention_mask.long().cumsum(-1) - 1
			position_ids.masked_fill_(attention_mask == 0, 1)
			if past_key_values:
				position_ids = position_ids[:, -input_ids.shape[1] :]

		# if `inputs_embeds` are passed, we only want to use them in the 1st generation step
		if inputs_embeds is not None and past_key_values is None:
			model_inputs = {"inputs_embeds": inputs_embeds}
		else:
			model_inputs = {"input_ids": input_ids}

		model_inputs.update(
			{
				"position_ids": position_ids,
				"past_key_values": past_key_values,
				"use_cache": kwargs.get("use_cache"),
				"attention_mask": attention_mask,
			}
		)
		return model_inputs

	@staticmethod
	def _reorder_cache(past_key_values, beam_idx):
		reordered_past = ()
		for layer_past in past_key_values:
			reordered_past += (
				tuple(
					past_state.index_select(0, beam_idx.to(past_state.device))
					for past_state in layer_past
				),
			)
		return reordered_past


@add_start_docstrings(
	"""
	The DeepseekV2 Model transformer with a sequence classification head on top (linear layer).

	[`DeepseekV2ForSequenceClassification`] uses the last token in order to do the classification, as other causal models
	(e.g. GPT-2) do.

	Since it does classification on the last token, it requires to know the position of the last token. If a
	`pad_token_id` is defined in the configuration, it finds the last token that is not a padding token in each row. If
	no `pad_token_id` is defined, it simply takes the last value in each row of the batch. Since it cannot guess the
	padding tokens when `inputs_embeds` are passed instead of `input_ids`, it does the same (take the last value in
	each row of the batch).
	""",
	DeepseekV2_START_DOCSTRING,
)
class DeepseekV2ForSequenceClassification(DeepseekV2PreTrainedModel):
	def __init__(self, config):
		super().__init__(config)
		self.num_labels = config.num_labels
		self.model = DeepseekV2Model(config)
		self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)

		# Initialize weights and apply final processing
		self.post_init()

	def get_input_embeddings(self):
		return self.model.embed_tokens

	def set_input_embeddings(self, value):
		self.model.embed_tokens = value

	@add_start_docstrings_to_model_forward(DeepseekV2_INPUTS_DOCSTRING)
	def forward(
		self,
		input_ids: torch.LongTensor = None,
		attention_mask: Optional[torch.Tensor] = None,
		position_ids: Optional[torch.LongTensor] = None,
		past_key_values: Optional[List[torch.FloatTensor]] = None,
		inputs_embeds: Optional[torch.FloatTensor] = None,
		labels: Optional[torch.LongTensor] = None,
		use_cache: Optional[bool] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		return_dict: Optional[bool] = None,
	) -> Union[Tuple, SequenceClassifierOutputWithPast]:
		r"""
		labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
			Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
			config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
			`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
		"""
		return_dict = (
			return_dict if return_dict is not None else self.config.return_dict
		)

		transformer_outputs = self.model(
			input_ids,
			attention_mask=attention_mask,
			position_ids=position_ids,
			past_key_values=past_key_values,
			inputs_embeds=inputs_embeds,
			use_cache=use_cache,
			output_attentions=output_attentions,
			output_hidden_states=output_hidden_states,
			return_dict=return_dict,
		)
		hidden_states = transformer_outputs[0]
		logits = self.score(hidden_states)

		if input_ids is not None:
			batch_size = input_ids.shape[0]
		else:
			batch_size = inputs_embeds.shape[0]

		if self.config.pad_token_id is None and batch_size != 1:
			raise ValueError(
				"Cannot handle batch sizes > 1 if no padding token is defined."
			)
		if self.config.pad_token_id is None:
			sequence_lengths = -1
		else:
			if input_ids is not None:
				sequence_lengths = (
					torch.eq(input_ids, self.config.pad_token_id).int().argmax(-1) - 1
				).to(logits.device)
			else:
				sequence_lengths = -1

		pooled_logits = logits[
			torch.arange(batch_size, device=logits.device), sequence_lengths
		]

		loss = None
		if labels is not None:
			labels = labels.to(logits.device)
			if self.config.problem_type is None:
				if self.num_labels == 1:
					self.config.problem_type = "regression"
				elif self.num_labels > 1 and (
					labels.dtype == torch.long or labels.dtype == torch.int
				):
					self.config.problem_type = "single_label_classification"
				else:
					self.config.problem_type = "multi_label_classification"

			if self.config.problem_type == "regression":
				loss_fct = MSELoss()
				if self.num_labels == 1:
					loss = loss_fct(pooled_logits.squeeze(), labels.squeeze())
				else:
					loss = loss_fct(pooled_logits, labels)
			elif self.config.problem_type == "single_label_classification":
				loss_fct = CrossEntropyLoss()
				loss = loss_fct(
					pooled_logits.view(-1, self.num_labels), labels.view(-1)
				)
			elif self.config.problem_type == "multi_label_classification":
				loss_fct = BCEWithLogitsLoss()
				loss = loss_fct(pooled_logits, labels)
		if not return_dict:
			output = (pooled_logits) #+ transformer_outputs[1:]
			return ((loss,) + output) if loss is not None else output

		return SequenceClassifierOutputWithPast(
			loss=loss,
			logits=pooled_logits,
			past_key_values=transformer_outputs.past_key_values,
			hidden_states=transformer_outputs.hidden_states,
			attentions=transformer_outputs.attentions,
		)


import json
import logging
from argparse import ArgumentParser
from pathlib import Path
from pprint import pformat
from typing import Dict, Iterable, Optional

import torch
import torch.nn.functional as F
from torch.nn.parallel import DataParallel
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import PreTrainedModel, PretrainedConfig, GPT2LMHeadModel, GPT2Tokenizer, top_k_top_p_filtering
from transformers.file_utils import ModelOutput

from .conv_data import ConversationalDataset, Collator, SpecialTokens, SPECIAL_TOKENS, add_special_tokens
from .log_utils import is_wandb_available, authorize_wandb


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
MAX_LENGTH = int(10000)  # Hardcoded max length to avoid infinite loop


def generate_no_beam_search(
    model: PreTrainedModel,
    input_ids: torch.LongTensor,
    decoder_input_ids: Optional[torch.LongTensor] = None,
    max_length: Optional[int] = None,
    min_length: Optional[int] = None,
    do_sample: Optional[bool] = None,
    temperature: Optional[float] = None,
    top_k: Optional[int] = None,
    top_p: Optional[float] = None,
    repetition_penalty: Optional[float] = None,
    bad_words_ids: Optional[Iterable[int]] = None,
    bos_token_id: Optional[int] = None,
    pad_token_id: Optional[int] = None,
    eos_token_id: Optional[int] = None,
    no_repeat_ngram_size: Optional[int] = None,
    num_return_sequences: Optional[int] = None,
    attention_mask: Optional[torch.LongTensor] = None,
    decoder_start_token_id: Optional[int] = None,
    use_cache: Optional[bool] = None,
    token_type_ids: Optional[torch.LongTensor] = None,
    **model_kwargs,
) -> Dict[str, torch.LongTensor]:
    """Generate sequences for each example without beam search (num_beams == 1).
    All returned sequence are generated independantly.
    """
    config: PretrainedConfig = getattr(model, "module", model).config

    max_length = max_length if max_length is not None else config.max_length
    min_length = min_length if min_length is not None else config.min_length
    do_sample = do_sample if do_sample is not None else config.do_sample
    use_cache = use_cache if use_cache is not None else config.use_cache
    temperature = temperature if temperature is not None else config.temperature
    top_k = top_k if top_k is not None else config.top_k
    top_p = top_p if top_p is not None else config.top_p
    repetition_penalty = repetition_penalty if repetition_penalty is not None else config.repetition_penalty
    bos_token_id = bos_token_id if bos_token_id is not None else config.bos_token_id
    pad_token_id = pad_token_id if pad_token_id is not None else config.pad_token_id
    eos_token_id = eos_token_id if eos_token_id is not None else config.eos_token_id
    no_repeat_ngram_size = no_repeat_ngram_size if no_repeat_ngram_size is not None else config.no_repeat_ngram_size
    bad_words_ids = bad_words_ids if bad_words_ids is not None else config.bad_words_ids
    num_return_sequences = num_return_sequences if num_return_sequences is not None else config.num_return_sequences
    decoder_start_token_id = (
        decoder_start_token_id if decoder_start_token_id is not None else config.decoder_start_token_id
    )

    batch_size = input_ids.shape[0]

    if (attention_mask is None) and (pad_token_id is not None) and (pad_token_id in input_ids):
        attention_mask = input_ids.ne(pad_token_id).long()
    elif attention_mask is None:
        attention_mask = input_ids.new_ones(input_ids.shape)

    if pad_token_id is None and eos_token_id is not None:
        logger.warning(
            "Setting `pad_token_id` to {} (first `eos_token_id`) to generate sequence".format(eos_token_id))
        pad_token_id = eos_token_id

    # set effective batch size and effective batch multiplier according to do_sample

    effective_batch_size = batch_size * num_return_sequences
    effective_batch_mult = num_return_sequences

    if config.is_encoder_decoder:
        if decoder_start_token_id is None:
            # see if BOS token can be used for decoder_start_token_id
            if bos_token_id is not None:
                decoder_start_token_id = bos_token_id
            elif (
                hasattr(config, "decoder")
                and hasattr(config.decoder, "bos_token_id")
                and config.decoder.bos_token_id is not None
            ):
                decoder_start_token_id = config.decoder.bos_token_id
            else:
                raise ValueError(
                    "decoder_start_token_id or bos_token_id has to be defined for encoder-decoder generation"
                )

        assert hasattr(
            model, "get_encoder"), "{} should have a 'get_encoder' function defined".format(model)
        assert callable(model.get_encoder), "{} should be a method".format(
            model.get_encoder)

        # get encoder and store encoder outputs
        encoder = model.get_encoder()
        encoder_outputs: ModelOutput = encoder(
            input_ids, attention_mask=attention_mask, return_dict=True)

    # Expand input ids if num_return_sequences > 1
    if num_return_sequences > 1:
        input_ids_len = input_ids.shape[-1]
        input_ids = input_ids.unsqueeze(1).expand(
            batch_size, effective_batch_mult, input_ids_len)
        attention_mask = attention_mask.unsqueeze(1).expand(
            batch_size, effective_batch_mult, input_ids_len)
        if token_type_ids is not None:
            token_type_ids = token_type_ids.unsqueeze(
                1).expand(-1, effective_batch_mult, input_ids_len)
            token_type_ids = token_type_ids.contiguous().view(
                effective_batch_size, input_ids_len)

        input_ids = input_ids.contiguous().view(
            effective_batch_size, input_ids_len
        )  # shape: (batch_size * num_return_sequences * num_beams, cur_len)
        attention_mask = attention_mask.contiguous().view(
            effective_batch_size, input_ids_len
        )  # shape: (batch_size * num_return_sequences * num_beams, cur_len)

    if config.is_encoder_decoder:
        device = next(model.parameters()).device
        if decoder_input_ids is not None:
            # give initial decoder input ids
            input_ids = decoder_input_ids.repeat(
                effective_batch_size, 1).to(device)
        else:
            # create empty decoder input_ids
            input_ids = torch.full(
                (effective_batch_size, 1),
                decoder_start_token_id,
                dtype=torch.long,
                device=device,
            )

        assert (
            batch_size == encoder_outputs.last_hidden_state.shape[0]
        ), f"expected encoder_outputs.last_hidden_state to have 1st dimension bs={batch_size}, got {encoder_outputs.last_hidden_state.shape[0]} "

        # expand batch_idx to assign correct encoder output for expanded input_ids (due to num_beams > 1 and num_return_sequences > 1)
        expanded_batch_idxs = (
            torch.arange(batch_size).view(-1, 1).repeat(1,
                                                        effective_batch_mult).view(-1).to(input_ids.device)
        )

        # expand encoder_outputs
        encoder_outputs["last_hidden_state"] = encoder_outputs.last_hidden_state.index_select(
            0, expanded_batch_idxs)

        # save encoder_outputs in `model_kwargs`
        model_kwargs["encoder_outputs"] = encoder_outputs

    input_lengths = (input_ids != pad_token_id).int().sum(-1) - 1

    # length of generated sentences / unfinished sentences
    unfinished_sents = input_ids.new(effective_batch_size).fill_(1)
    sent_lengths = input_ids.new(effective_batch_size).fill_(max_length)

    generated_ids = input_ids[torch.arange(
        effective_batch_size), input_lengths].unsqueeze(-1)

    if token_type_ids is not None:
        generated_token_types = token_type_ids[torch.arange(
            effective_batch_size), input_lengths].unsqueeze(-1)

    past = None
    for cur_len in range(max_length):
        model_inputs = model.prepare_inputs_for_generation(
            input_ids, past=past, attention_mask=attention_mask, use_cache=use_cache, **model_kwargs
        )

        if token_type_ids is not None:
            if past:
                model_inputs["token_type_ids"] = token_type_ids[:, -
                                                                1].unsqueeze(-1)
            else:
                model_inputs["token_type_ids"] = token_type_ids

        outputs = model(**model_inputs, return_dict=True)
        if cur_len == 0:
            next_token_logits = outputs.logits[torch.arange(
                effective_batch_size), input_lengths, :]
        else:
            next_token_logits = outputs.logits[:, -1, :]

        scores = model.postprocess_next_token_scores(
            scores=next_token_logits,
            input_ids=input_ids,
            no_repeat_ngram_size=no_repeat_ngram_size,
            bad_words_ids=bad_words_ids,
            cur_len=cur_len,
            min_length=min_length,
            max_length=max_length,
            eos_token_id=eos_token_id,
            repetition_penalty=repetition_penalty,
            batch_size=batch_size,
            num_beams=1,
        )

        # if model has past, then set the past variable to speed up decoding
        if "past_key_values" in outputs:
            past = outputs.past_key_values
        elif "mems" in outputs:
            past = outputs.mems

        if do_sample:
            # Temperature (higher temperature => more likely to sample low probability tokens)
            if temperature != 1.0:
                scores = scores / temperature
            # Top-p/top-k filtering
            next_token_logscores = top_k_top_p_filtering(
                scores, top_k=top_k, top_p=top_p)
            # Sample
            probs = F.softmax(next_token_logscores, dim=-1)
            next_token = torch.multinomial(probs, num_samples=1).squeeze(-1)
        else:
            # Greedy decoding
            if cur_len == 0:
                _, next_token = torch.topk(
                    next_token_logits.view(
                        batch_size, effective_batch_mult, -1)[:, 0, :],
                    k=effective_batch_mult,
                    dim=-1,
                )
                next_token = next_token.reshape(
                    effective_batch_size, -1).squeeze(-1)
            else:
                next_token = torch.argmax(next_token_logits, dim=-1)

        # update generations and finished sentences
        if eos_token_id is not None:
            # pad finished sentences if eos_token_id exist
            tokens_to_add = next_token * unfinished_sents + \
                (pad_token_id) * (1 - unfinished_sents)
        else:
            tokens_to_add = next_token

        # add token and increase length by one
        if token_type_ids is not None:
            next_token_types = torch.gather(token_type_ids, dim=1, index=input_lengths.unsqueeze(-1)).squeeze(
                -1
            ) * unfinished_sents + pad_token_id * (1 - unfinished_sents)
        next_len = cur_len + 1
        input_lengths = input_lengths + 1
        input_ids = torch.cat([input_ids, tokens_to_add.unsqueeze(-1)], dim=-1)

        generated_ids = torch.cat(
            [generated_ids, tokens_to_add.unsqueeze(-1)], dim=-1)

        if token_type_ids is not None:
            token_type_ids = torch.cat(
                [token_type_ids, next_token_types.unsqueeze(-1)], dim=-1)
            generated_token_types = torch.cat(
                [generated_token_types, next_token_types.unsqueeze(-1)], dim=-1)

        if eos_token_id is not None:
            eos_in_sents = tokens_to_add == eos_token_id
            # if sentence is unfinished and the token to add is eos, sent_lengths is filled with current length
            is_sents_unfinished_and_token_to_add_is_eos = unfinished_sents.mul(
                eos_in_sents.long()).bool()
            sent_lengths.masked_fill_(
                is_sents_unfinished_and_token_to_add_is_eos, next_len)
            # unfinished_sents is set to zero if eos in sentence
            unfinished_sents.mul_((~eos_in_sents).long())

        # stop when there is a </s> in each sentence, or if we exceed the maximul length
        if unfinished_sents.max() == 0:
            break

        # extend attention_mask for new generated input if only decoder
        if model.config.is_encoder_decoder is False:
            attention_mask = torch.cat(
                [attention_mask, attention_mask.new_ones((attention_mask.shape[0], 1))], dim=-1)

    output = dict(input_ids=input_ids, generated_ids=generated_ids,
                  attention_mask=attention_mask)
    if token_type_ids is not None:
        output["token_type_ids"] = token_type_ids
        output["generated_token_types"] = generated_token_types

    return output

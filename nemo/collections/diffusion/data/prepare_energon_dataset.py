# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import pickle
from typing import Callable, List
import pandas as pd
import nemo_run as run
import numpy as np
import torch
import torch.distributed as dist
import webdataset as wds
from einops import rearrange
from transformers import T5EncoderModel, T5TokenizerFast
from nemo.collections.common.video_tokenizers.cosmos_tokenizer import CausalVideoTokenizer
from nemo.collections.common.video_tokenizers.utils import (
    read_image,
    resize_video,
)

def initialize_text_encoder(t5_cache_dir):
    # Load tokenizer and text encoder, save in cache directory
    tokenizer = T5TokenizerFast.from_pretrained("google-t5/t5-11b", cache_dir=t5_cache_dir)
    text_encoder = T5EncoderModel.from_pretrained("google-t5/t5-11b", cache_dir=t5_cache_dir)
    text_encoder.to("cuda")
    text_encoder.eval()

    return tokenizer, text_encoder

# Load dataset from HuggingFace
df = pd.read_parquet("hf://datasets/huggan/smithsonian_butterflies_subset/data/train-00000-of-00001.parquet")
# Load Cosmos tokenizer from HuggingFace
autoencoder = CausalVideoTokenizer.from_pretrained("CosmosCausalCV_f4x8x8")
# Load T5-XXL text encoder
t5_cache_dir = '' # Use your own custom cache path
tokenizer, text_encoder = initialize_text_encoder(t5_cache_dir)

class EncodedSample:
    def __init__(
        self,
        encoded_text: np.ndarray, 
        length: int, 
        attn_mask: np.ndarray, 
        offset_mappings: np.ndarray
    ):
        self.encoded_text = encoded_text
        self.length = length
        self.attn_mask = attn_mask
        self.offset_mappings = offset_mappings

    def truncate(self) -> None:
        self.encoded_text = self.encoded_text[0 : self.length].astype(np.float16)
        self.attn_mask = self.attn_mask[0 : self.length].astype(np.int32)
        if self.offset_mappings is not None:
            self.offset_mappings = self.offset_mappings[0 : self.length].astype(np.int32)

@torch.no_grad()
def encode_for_batch(
    tokenizer, encoder, prompts: list[str], truncate: bool = True, max_length=512, output_mapping=True
):
    """
    encode a batch of text prompts to a batch of T5 embedding
    Parameters:
        tokenizer: t5 embedding tokenizer.
        text_encoder: t5 embedding text_encoder.
        prompts: a batch of text prompts
        truncate: whether to truncate the T5 embedding
    """
    batch_encoding = tokenizer.batch_encode_plus(
        prompts,
        return_tensors="pt",
        truncation=True,
        padding="max_length",
        max_length=max_length,
        return_length=True,
        return_offsets_mapping=output_mapping,
    )

    # We expect all the processing is done in GPU.
    input_ids = batch_encoding.input_ids.cuda()
    attn_mask = batch_encoding.attention_mask.cuda()
    if output_mapping:
        offsets_mapping = batch_encoding["offset_mapping"]
        offsets_mapping = offsets_mapping.cpu().numpy()
    else:
        offsets_mapping = None

    outputs = encoder(input_ids=input_ids, attention_mask=attn_mask)  # type: ignore
    encoded_text = outputs.last_hidden_state

    lengths = attn_mask.sum(dim=1).cpu()
    for batch_id in range(encoded_text.shape[0]):
        encoded_text[batch_id][lengths[batch_id] :] = 0

    encoded_text = encoded_text.cpu().numpy()
    attn_mask = attn_mask.cpu().numpy()

    encoded_text = encoded_text[:, :max_length]
    attn_mask = attn_mask[:, :max_length]

    out = []
    for idx in range(encoded_text.shape[0]):
        if output_mapping:
            offsets = offsets_mapping[idx]
        else:
            offsets = None

        out.append(EncodedSample(encoded_text[idx].astype(np.float16), lengths[idx], attn_mask[idx], offsets))
    if truncate:
        for x in out:
            x.truncate()
    return out

def generate_t5_embed(tokenizer, text_encoder, prompt, t5_embeding_max_length=512):
    # encode text to t5 embedding
    out = encode_for_batch(tokenizer, text_encoder, [prompt])[0]
    encoded_text = torch.tensor(out.encoded_text, dtype=torch.bfloat16)

    # padding t5 embedding to t5_embeding_max_length
    L, C = encoded_text.shape
    t5_embed = torch.zeros(1, t5_embeding_max_length, C, dtype=torch.bfloat16)
    t5_embed[0, :L] = encoded_text

    return t5_embed

def get_start_end_idx_for_this_rank(dataset_size, rank, world_size):
    """
    Calculate the start and end indices for a given rank in a distributed setting.

    Args:
        dataset_size (int): The total size of the dataset.
        rank (int): The rank of the current process.
        world_size (int): The total number of processes.

    Returns:
        tuple: A tuple containing the start index (int) and end index (int) for the given rank.
    """
    split_size = dataset_size // world_size
    start_idx = rank * split_size
    # The last rank takes the remainder
    end_idx = start_idx + split_size if rank != world_size - 1 else dataset_size
    return start_idx, end_idx

def butterfly_process_func(index):
    """
    Generates a sample dictionary with image latent tensor, image caption, and metadata.
    """
    # Access the data from the dataframe
    row = df.iloc[index]
    image_url = row["image_url"]
    image_caption = row["name"]

    # Process image
    video = read_image(image_url)
    video = rearrange(video, 'h w (t c) -> t h w c', t=1)
    video = resize_video(video, short_size=512)
    batch_video = video[np.newaxis, ...]
    
    # Run autoencoder to get latents
    _, image_latent = autoencoder(batch_video, temporal_window=1)

    text_embedding = generate_t5_embed(tokenizer, text_encoder, image_caption)

    # Construct sample dictionary
    sample = {
        "__key__": f"{index:06}",
        ".pth": image_latent.to(dtype=torch.bfloat16),
        ".pickle": pickle.dumps(text_embedding),
        ".json": {
            "image_height": batch_video.shape[2],
            "image_width": batch_video.shape[3],
            # Add additional score as metadata
        },
    }
    return sample


@torch.no_grad()
@run.cli.entrypoint
def prepare(process_func: Callable, output_dir: str = 'output'):
    """
    Distributed preparation of the WebDataset using the provided processing function.
    """
    rank = dist.get_rank()
    world_size = torch.distributed.get_world_size()

    start_idx, end_idx = get_start_end_idx_for_this_rank(len(df), rank, world_size)
    os.makedirs(output_dir, exist_ok=True)
    output_tar = os.path.join(output_dir, f"rank{rank}-%06d.tar")
    
    with wds.ShardWriter(output_tar, maxcount=10000) as sink:
        for i in range(start_idx, end_idx):
            sample = process_func(i)
            # Write sample to tar file
            sink.write(sample)


@run.cli.factory(target=prepare)
def prepare_butterfly_dataset() -> run.Partial:
    recipe = run.Partial(
        prepare,
        process_func=butterfly_process_func,
        output_dir='butterfly_webdataset'
    )
    return recipe

if __name__ == '__main__':
    dist.init_process_group("nccl")
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    run.cli.main(prepare, default_factory=prepare_butterfly_dataset)

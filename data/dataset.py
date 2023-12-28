import itertools
import numpy as np
import random
import torch
import json

from pathlib import Path
from torch.utils.data import Dataset, get_worker_info
from data.store import DirectoryImageStore, LatentStore

image_suffix = set([".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"])


def is_latent_folder(path: Path):
    # iterate over all files in the folder and find if any of them is a latent
    for p in path.iterdir():
        if p.is_dir():
            continue
        if p.suffix == ".h5":
            return True


class AspectRatioDataset(Dataset):
    def __init__(
        self,
        batch_size: int,
        img_path: Path | str,
        ucg: int = 0,
        rank: int = 0,
        prompt_processor: str | None = None,
        dtype=torch.float16,
        use_central_crop=False,
        target_area: int = 1024 * 1024,
        min_size: int = 512,
        max_size: int = 2048,
        divisible: int = 64,
        base_len: int = 1024,
        seed: int = 42,
        **kwargs,
    ):
        self.rng = np.random.default_rng(seed)
        self.batch_size = batch_size
        root_path = Path(img_path)
        assert root_path.exists()

        store_class = DirectoryImageStore
        prompt_mapping = None
        if is_latent_folder(root_path):
            prompt_mapping = Path("dataset.json")
            if not prompt_mapping.exists():
                prompt_mapping = Path(root_path.parent / "dataset.json")

            assert prompt_mapping.exists()
            prompt_mapping = json.loads(prompt_mapping.read_text())
            store_class = LatentStore

        self.store = store_class(
            root_path,
            rank=rank,
            ucg=ucg,
            prompt_mapping=prompt_mapping,
            prompt_processor=prompt_processor,
            use_central_crop=use_central_crop,
            dtype=dtype,
            base_len=base_len,
            **kwargs,
        )

        self.target_area = target_area
        self.base_len = base_len
        self.max_size, self.min_size, self.divisible = max_size, min_size, divisible
        self._length = int(np.ceil(self.store.length / self.batch_size))
        self.generate_buckets()
        self.first_time = True

    def __len__(self):
        return self._length

    def generate_buckets(self):
        assert (
            self.target_area % 4096 == 0
        ), "target area (h * w) must be divisible by 64"
        width = np.arange(self.min_size, self.max_size + 1, self.divisible)
        height = np.minimum(self.max_size, ((self.target_area // width) // self.divisible) * self.divisible,)
        valid_mask = height >= self.min_size

        resos = set(zip(width[valid_mask], height[valid_mask]))
        resos.update(zip(height[valid_mask], width[valid_mask]))
        resos.add(((int(np.sqrt(self.target_area)) // self.divisible) * self.divisible,) * 2)
        self.buckets_sizes = np.array(sorted(resos))
        self.bucket_ratios = self.buckets_sizes[:, 0] / self.buckets_sizes[:, 1]

    def assign_buckets(self):
        # self.store.fix_aspect_randomness(self.rng)
        img_res = np.array(self.store.raw_res)
        img_ratios = img_res[:, 0] / img_res[:, 1]
        img_idxs = np.argsort(img_ratios)
        landscape_idxs = img_idxs[img_ratios[img_idxs] <= 1]
        portrait_idxs = img_idxs[img_ratios[img_idxs] > 1]
        self.bucket_content = [[] for _ in range(len(self.buckets_sizes))]

        # Initial assignment, images are rounded towards the base bucket
        bucket_idx = 0

        self.store.to_ratio = np.empty(self.store.length)
        idx_size = landscape_idxs.size
        reminder = idx_size % self.batch_size

        it = []
        if idx_size >= self.batch_size:
            it = np.split(landscape_idxs[:-reminder], idx_size // self.batch_size)

        if reminder:
            it.append(landscape_idxs[-reminder:])

        for idx_chunk in it:
            idx = idx_chunk[-1]
            while self.bucket_ratios[bucket_idx] < img_ratios[idx]:
                bucket_idx += 1
            self.bucket_content[bucket_idx].extend(idx_chunk)
            self.store.to_ratio[idx_chunk] = self.bucket_ratios[bucket_idx]

        idx_size = portrait_idxs.size
        reminder = idx_size % self.batch_size

        it = []
        if idx_size >= self.batch_size:
            it = np.split(portrait_idxs[reminder:], idx_size // self.batch_size)[::-1]

        if reminder:
            it.append(portrait_idxs[:reminder])

        bucket_idx = len(self.buckets_sizes) - 1
        for idx_chunk in it:
            idx = idx_chunk[0]
            while self.bucket_ratios[bucket_idx] > img_ratios[idx]:
                bucket_idx -= 1
            self.bucket_content[bucket_idx].extend(idx_chunk)
            self.store.to_ratio[idx_chunk] = self.bucket_ratios[bucket_idx]

    def assign_batches(self):
        self.batch_idxs = []
        for bucket in self.bucket_content:
            if not bucket:
                continue
            reminder = len(bucket) % self.batch_size
            bucket = np.array(bucket)
            self.rng.shuffle(bucket)
            if not reminder:
                self.batch_idxs.extend(bucket.reshape(-1, self.batch_size))
            else:
                self.batch_idxs.extend(bucket[:-reminder].reshape(-1, self.batch_size))
                self.batch_idxs.append(bucket[-reminder:])
        np.random.shuffle(self.batch_idxs)

    def put_most_oom_like_batch_first(self):
        idx = next(
            idx
            for b in itertools.chain(self.bucket_content, reversed(self.bucket_content))
            for idx in b
            if b
        )
        i = next(i for i, batch_idxs in enumerate(self.batch_idxs) if idx in batch_idxs)
        self.batch_idxs[0], self.batch_idxs[i] = self.batch_idxs[i], self.batch_idxs[0]

    def __getitem__(self, idx):
        img_idxs = self.batch_idxs[idx]
        return self.store.get_batch(img_idxs)


def worker_init_fn(worker_id):
    worker_info = get_worker_info()
    dataset: AspectRatioDataset = worker_info.dataset  # type: ignore
    random.seed(worker_info.seed)  # type: ignore
    dataset.assign_buckets()
    dataset.assign_batches()
    if dataset.first_time:
        dataset.put_most_oom_like_batch_first()
        dataset.first_time = False

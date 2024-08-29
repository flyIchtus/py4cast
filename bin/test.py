"""Tests a trained model.
Takes a path to the checkpoint of a trained model, computes metrics on the test set and
saves scores plots and forecast animations in the log folder of the model.
You can change the number of auto-regressive steps with option `num_pred_steps` to
make long forecasts.
"""

from argparse import ArgumentParser
from pathlib import Path

import pytorch_lightning as pl
from lightning.pytorch.loggers import TensorBoardLogger

from py4cast.datasets import get_datasets
from py4cast.datasets.base import TorchDataloaderSettings
from py4cast.lightning import AutoRegressiveLightning
from py4cast.settings import ROOTDIR


def get_log_dirs(path: Path):
    """Retrieves log folders of the checkpoint's run."""
    log_dir = ROOTDIR / "logs"
    subfolder = path.parent.name
    folder = Path(path.parents[3].name) / path.parents[2].name / path.parents[1].name
    return log_dir, folder, subfolder


parser = ArgumentParser(
    description="Inference on test dataset for weather forecasting."
)
parser.add_argument("ckpt_path", type=Path, help="Path to model checkpoint.")
parser.add_argument(
    "--num_pred_steps",
    type=int,
    default=3,
    help="Number of auto-regressive steps/prediction steps.",
)
args = parser.parse_args()

print(f"Loading model {args.ckpt_path}...")
model = AutoRegressiveLightning.load_from_checkpoint(args.ckpt_path)
model.eval()

# Change number of auto regresive steps for long forecasts
print(f"Changing number of val pred steps to {args.num_pred_steps}...")
hparams = model.hparams["hparams"]
hparams.num_pred_steps_val_test = args.num_pred_steps

log_dir, folder, subfolder = get_log_dirs(args.ckpt_path)
logger = TensorBoardLogger(
    save_dir=log_dir, name=folder, version=subfolder, default_hp_metric=False
)

trainer = pl.Trainer(logger=logger, devices="auto")

# Initializing data loader
dl_settings = TorchDataloaderSettings(batch_size=2, num_workers=5, prefetch_factor=2)
_, val_ds, _ = get_datasets(
    hparams.dataset_name,
    hparams.num_input_steps,
    hparams.num_pred_steps_train,
    hparams.num_pred_steps_val_test,
    hparams.dataset_conf,
)
dataloader = val_ds.torch_dataloader(dl_settings)

print("Testing...")
trainer.test(model=model, dataloaders=val_ds.torch_dataloader(dl_settings))
# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0
"""Example script to train a DeepLabv3 model on ADE20k."""

import os
import sys

import torch
from composer import Trainer
from composer.algorithms import EMA, SAM, ChannelsLast, MixUp
from composer.callbacks import LRMonitor, MemoryMonitor, SpeedMonitor
from composer.loggers import ProgressBarLogger, WandBLogger
from composer.optim import CosineAnnealingScheduler, DecoupledSGDW
from composer.utils import dist
from data import build_ade20k_dataspec, build_streaming_ade20k_dataspec
from model import build_composer_deeplabv3
from omegaconf import OmegaConf


def build_logger(name, kwargs):
    if name == 'progress_bar':
        return ProgressBarLogger(
            progress_bar=kwargs.get('progress_bar', True),
            log_to_console=kwargs.get('log_to_console', True),
        )
    elif name == 'wandb':
        return WandBLogger(**kwargs)
    else:
        raise ValueError(f'Not sure how to build logger: {name}')


def log_config(cfg):
    print(OmegaConf.to_yaml(cfg))
    if 'wandb' in cfg.loggers:
        try:
            import wandb
        except ImportError as e:
            raise e
        if wandb.run:
            wandb.config.update(OmegaConf.to_container(cfg, resolve=True))


def main(config):
    # update trainer settings based on selected recipe
    if config.recipe_name:
        recipe_settings = config[config.recipe]
        config.update(recipe_settings)
    # Divide batch sizes by number of devices if running multi-gpu training
    train_batch_size = config.train_dataset.batch_size
    eval_batch_size = config.eval_dataset.batch_size
    

    if dist.get_world_size():
        train_batch_size //= dist.get_world_size()
        eval_batch_size //= dist.get_world_size()


    # Train dataset
    print('Building train dataloader')
    if config.train_dataset.is_streaming:
        train_dataspec = build_streaming_ade20k_dataspec(
            remote=config.train_dataset.path,
            local=config.train_dataset.local,
            batch_size=train_batch_size,
            split='train',
            drop_last=True,
            shuffle=True,
            base_size=config.train_dataset.base_size,
            min_resize_scale=config.train_dataset.min_resize_scale,
            max_resize_scale=config.train_dataset.max_resize_scale,
            final_size=config.train_dataset.final_size,
            ignore_background=config.train_dataset.ignore_background,
            num_workers=8,
            pin_memory=True,
            persistent_workers=True)
    else:
        train_dataspec = build_ade20k_dataspec(
            datadir=config.train_dataset.path,
            batch_size=train_batch_size,
            split='train',
            drop_last=True,
            shuffle=True,
            base_size=config.train_dataset.base_size,
            min_resize_scale=config.train_dataset.min_resize_scale,
            max_resize_scale=config.train_dataset.max_resize_scale,
            final_size=config.train_dataset.final_size,
            ignore_background=config.train_dataset.ignore_background,
            num_workers=8,
            pin_memory=True,
            persistent_workers=True)

    print('Built train dataloader\n')

    # Validation dataset
    print('Building evaluation dataloader')
    if config.eval_dataset.is_streaming:
        eval_dataspec = build_streaming_ade20k_dataspec(
            remote=config.eval_dataset.path,
            local=config.eval_dataset.local,
            batch_size=eval_batch_size,
            split='val',
            drop_last=True,
            shuffle=True,
            base_size=config.train_dataset.base_size,
            min_resize_scale=config.train_dataset.min_resize_scale,
            max_resize_scale=config.train_dataset.max_resize_scale,
            final_size=config.train_dataset.final_size,
            ignore_background=config.train_dataset.ignore_background,
            num_workers=8,
            pin_memory=True,
            persistent_workers=True)
    else:
        eval_dataspec = build_ade20k_dataspec(
            datadir=config.eval_dataset.path,
            batch_size=eval_batch_size,
            split='val',
            drop_last=True,
            shuffle=True,
            base_size=config.train_dataset.base_size,
            min_resize_scale=config.train_dataset.min_resize_scale,
            max_resize_scale=config.train_dataset.max_resize_scale,
            final_size=config.train_dataset.final_size,
            ignore_background=config.train_dataset.ignore_background,
            num_workers=8,
            pin_memory=True,
            persistent_workers=True)
    print('Built evaluation dataloader\n')

    # Instantiate Deeplab model
    print('Building Composer model')

    def weight_init(module: torch.nn.Module):
        if isinstance(module, (torch.nn.Linear, torch.nn.Conv2d)):
            torch.nn.init.kaiming_normal_(module.weight)
        if isinstance(module, torch.nn.BatchNorm2d):
            torch.nn.init.ones_(module.weight)
            torch.nn.init.zeros_(module.bias)

    composer_model = build_composer_deeplabv3(
        num_classes=config.model.num_classes,
        backbone_arch=config.model.backbone_arch,
        backbone_weights=config.model.backbone_weights,
        sync_bn=config.model.sync_bn,
        use_plus=True,
        cross_entropy_weight=config.model.cross_entropy_weight,
        dice_weight=config.model.dice_weight,
        init_fn=weight_init)

    print('Built Composer model\n')

    # Optimizer
    print('Building optimizer and learning rate scheduler')
    optimizer = DecoupledSGDW(composer_model.parameters(),
                              lr=config.optimizer.lr,
                              momentum=config.optimizer.momentum,
                              weight_decay=config.optimizer.weight_decay)

    # Only use a LR schedule if no recipe is specified or if the hot recipe was specified
    lr_scheduler = None
    if config.recipe_name is None or config.recipe_name == 'hot':
        lr_scheduler = CosineAnnealingScheduler()

    print('Built optimizer and learning rate scheduler\n')

    # Callbacks for logging
    print('Building Speed, LR, and Memory monitoring callbacks')
    speed_monitor = SpeedMonitor(
        window_size=50
    )  # Measures throughput as samples/sec and tracks total training time
    lr_monitor = LRMonitor()  # Logs the learning rate
    memory_monitor = MemoryMonitor()  # Logs memory utilization

    # Callback for checkpointing
    print('Built Speed, LR, and Memory monitoring callbacks\n')

    # Recipes for training ResNet architectures on ImageNet in order of increasing training time and accuracy
    # To learn about individual methods, check out "Methods Overview" in our documentation: https://docs.mosaicml.com/
    print('Building algorithm recipes')

    if config.recipe_name == 'mild':
        algorithms = [
            ChannelsLast(),
            EMA(half_life='1000ba', update_interval='10ba'),
        ]

    elif config.recipe_name == 'medium':
        algorithms = [
            ChannelsLast(),
            EMA(half_life='1000ba', update_interval='10ba'),
            SAM(rho=0.3, interval=2),
            MixUp(alpha=0.2),
        ]

    elif config.recipe_name == 'hot':
        algorithms = [
            ChannelsLast(),
            EMA(half_life='2000ba', update_interval='1ba'),
            SAM(rho=0.3, interval=1),
            MixUp(alpha=0.5),
        ]

    else:
        algorithms = None

    print('Built algorithm recipes\n')

    loggers = [
        build_logger(name, logger_config)
        for name, logger_config in config.loggers.items()
    ]

    # Create the Trainer!
    print('Building Trainer')
    device = 'gpu' if torch.cuda.is_available() else 'cpu'
    precision = 'amp' if device == 'gpu' else 'fp32'  # Mixed precision for fast training when using a GPU
    trainer = Trainer(
        run_name=config.run_name,
        model=composer_model,
        train_dataloader=train_dataspec,
        eval_dataloader=eval_dataspec,
        eval_interval='1ep',
        optimizers=optimizer,
        schedulers=lr_scheduler,
        algorithms=algorithms,
        loggers=loggers,
        max_duration=config.max_duration,
        callbacks=[speed_monitor, lr_monitor, memory_monitor],
        save_folder=config.save_folder,
        save_interval=config.save_interval,
        save_num_checkpoints_to_keep=config.save_num_checkpoints_to_keep,
        load_path=config.load_path,
        device=device,
        precision=precision,
        grad_accum=config.grad_accum,
        seed=config.seed)
    print('Built Trainer\n')

    print('Logging config')
    log_config(config)

    print('Run evaluation')
    trainer.eval()
    if config.is_train:
        print('Train!')
        trainer.fit()


if __name__ == '__main__':
    #print(sys.argv[1], os.path.exists(sys.argv[1]))
    if len(sys.argv) < 2 or not os.path.exists(sys.argv[1]):
        raise ValueError('The first argument must be a path to a yaml config.')

    yaml_path, args_list = sys.argv[1], sys.argv[2:]
    with open(yaml_path) as f:
        yaml_config = OmegaConf.load(f)
    cli_config = OmegaConf.from_cli(args_list)
    config = OmegaConf.merge(yaml_config, cli_config)
    main(config)

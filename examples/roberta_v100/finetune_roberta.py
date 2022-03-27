import os

import torch
import argparse
import numpy as np

from SwissArmyTransformer import mpu, get_args
from SwissArmyTransformer\
    .training.deepspeed_training import training_main, initialize_distributed, load_checkpoint
from roberta_model import RobertaModel, LoRAMixin
from SwissArmyTransformer.model.mixins import PrefixTuningMixin, BaseMixin
from functools import partial
from utils import create_dataset_function, ChildTuningAdamW, set_optimizer_mask

class MLPHeadMixin(BaseMixin):
    def __init__(self, hidden_size, *output_sizes, bias=True, activation_func=torch.nn.functional.relu, init_mean=0, init_std=0.005, old_model=None):
        super().__init__()
        self.activation_func = activation_func
        last_size = hidden_size
        self.layers = torch.nn.ModuleList()
        for i, sz in enumerate(output_sizes):
            this_layer = torch.nn.Linear(last_size, sz, bias=bias)
            last_size = sz
            if old_model is None:
                torch.nn.init.normal_(this_layer.weight, mean=init_mean, std=init_std)
            else:
                old_weights = old_model.mixins["classification_head"].layers[i].weight.data
                this_layer.weight.data.copy_(old_weights)
            self.layers.append(this_layer)

    def final_forward(self, logits, **kw_args):
        for i, layer in enumerate(self.layers):
            if i > 0:
                logits = self.activation_func(logits)
            logits = layer(logits)
        return logits

class ClassificationModel(RobertaModel):
    def __init__(self, args, transformer=None, parallel_output=True):
        super().__init__(args, transformer=transformer, parallel_output=parallel_output)
        self.del_mixin('roberta-final')
        self.add_mixin('classification_head', MLPHeadMixin(args.hidden_size, 2048, 1))
        self.finetune_type = args.finetune_type
        if 'pt' in self.finetune_type:
            print('Add prefix tuning mixin')
            self.add_mixin('prefix-tuning', PrefixTuningMixin(args.num_layers, args.hidden_size // args.num_attention_heads, args.num_attention_heads, args.prefix_len))
        if 'lora' in self.finetune_type:
            print('Add lora mixin')
            self.add_mixin('lora', LoRAMixin(args.hidden_size, args.num_layers, args.lora_r, args.lora_alpha, args.lora_dropout))

    def disable_untrainable_params(self):
        if not 'all' in self.finetune_type:
            print('froze model parameter')
            self.transformer.requires_grad_(False)

        if 'bitfit' in self.finetune_type:
            print('Use bitfit')
            for layer_id in range(len(self.transformer.layers)):
                # self.transformer.layers[layer_id].mlp.dense_h_to_4h.requires_grad_(True) #Wm2
                # self.transformer.layers[layer_id].attention.dense.requires_grad_(True) #Wm1
                # self.transformer.layers[layer_id].attention.query_key_value.requires_grad_(True) #QKV
                self.transformer.layers[layer_id].mlp.dense_h_to_4h.bias.requires_grad_(True) #b_m2
                self.transformer.layers[layer_id].attention.query_key_value.bias.requires_grad_(True) #b_qkv

    def get_optimizer(self, args, train_data):
        optimizer_kwargs = {
            "betas": (0.9, 0.98),
            "eps": 1e-6,
        }
        optimizer_kwargs["lr"] = args.lr
        optimizer = partial(ChildTuningAdamW, reserve_p=args.reserve_p, mode=args.child_type, **optimizer_kwargs)
        return optimizer


def get_batch(data_iterator, args, timers):
    # Items and their type.
    keys = ['input_ids', 'position_ids', 'attention_mask', 'label']
    datatype = torch.int64

    # Broadcast data.
    timers('data loader').start()
    if data_iterator is not None:
        data = next(data_iterator)
    else:
        data = None
    timers('data loader').stop()
    data_b = mpu.broadcast_data(keys, data, datatype)
    # Unpack.
    tokens = data_b['input_ids'].long()
    labels = data_b['label'].long()
    position_ids = data_b['position_ids'].long()
    attention_mask = data_b['attention_mask'][:, None, None, :].float()

    # Convert
    if args.fp16:
        attention_mask = attention_mask.half()
    
    return tokens, labels, attention_mask, position_ids, (tokens!=1)


def forward_step(data_iterator, model, args, timers):
    """Forward step."""

    # Get the batch.
    timers('batch generator').start()
    tokens, labels, attention_mask, position_ids, loss_mask = get_batch(
        data_iterator, args, timers)
    timers('batch generator').stop()

    logits, *mems = model(tokens, position_ids, attention_mask)
    # pred = ((logits.contiguous().float().squeeze(-1)) * loss_mask).sum(dim=-1) / loss_mask.sum(dim=-1)
    pred = logits.contiguous().float().squeeze(-1)[..., 0]
    loss = torch.nn.functional.binary_cross_entropy_with_logits(
        pred,
        labels.float()
    )
    acc = ((pred > 0.).long() == labels).sum() / labels.numel()
    return loss, {'acc': acc}


if __name__ == '__main__':
    py_parser = argparse.ArgumentParser(add_help=False)
    py_parser.add_argument('--new_hyperparam', type=str, default=None)
    py_parser.add_argument('--sample_length', type=int, default=512-16)

    #type
    py_parser.add_argument('--finetune-type', type=str, default="all")

    #pt
    py_parser.add_argument('--prefix_len', type=int, default=16)
    py_parser.add_argument('--old_checkpoint', action="store_true")
    py_parser.add_argument('--dataset-name', type=str, required=True)

    #lora
    py_parser.add_argument('--lora-r', type=int, default=8)
    py_parser.add_argument('--lora-alpha', type=float, default=16)
    py_parser.add_argument('--lora-dropout', type=str, default=None)

    #child
    py_parser.add_argument('--child-type', type=str, default="ChildTuning-D")
    py_parser.add_argument('--reserve-p', type=float, default=0.3)
    py_parser.add_argument('--max-grad-norm', type=float, default=1.0)
    py_parser.add_argument('--child-load', type=str, default=None)

    #old_model
    py_parser.add_argument('--head-load', action="store_true")
    py_parser.add_argument('--head-path', type=str, default=None)


    known, args_list = py_parser.parse_known_args()
    args = get_args(args_list)
    args = argparse.Namespace(**vars(args), **vars(known))

    #print information

    print(f"*******************Experiment Name is {args.experiment_name}****************************")
    print(f"*******************Finetune Type is {args.finetune_type}****************************")
    print(f"*******************Learning Rate is {args.lr}****************************")


    if 'child' in args.finetune_type:
        if args.child_load is not None:
            args.load = args.child_load
        training_main(args, model_cls=ClassificationModel, forward_step_function=forward_step, create_dataset_function=create_dataset_function, get_optimizer_from_model=True, set_optimizer_mask=set_optimizer_mask)
    elif args.head_load:
        args.old_model = None
        load = args.load
        args.load = args.head_path
        initialize_distributed(args)
        old_model = ClassificationModel(args)
        args.do_train=True
        _ = load_checkpoint(old_model, args)
        old_model.requires_grad_(False)
        if args.fp16:
            old_model.half()
        elif args.bf16:
            old_model.bfloat16()
        old_model.cuda(torch.cuda.current_device())
        args.old_model = old_model
        training_main(args, model_cls=ClassificationModel, forward_step_function=forward_step, create_dataset_function=create_dataset_function, already_init=True)
    else:
        training_main(args, model_cls=ClassificationModel, forward_step_function=forward_step, create_dataset_function=create_dataset_function)

    # args.load = "/workspace/yzy/ST_develop/SwissArmyTransformer/examples/roberta_test/checkpoints/finetune-roberta-large-boolq-lora-1e-4-03-18-12-27"
    # args.load = "/workspace/yzy/ST_develop/SwissArmyTransformer/examples/roberta_test/checkpoints/finetune-roberta-large-boolq-bitfit-1e-3-03-08-13-15"
    # args.load = "/workspace/yzy/ST_develop/SwissArmyTransformer/examples/roberta_test/checkpoints/finetune-roberta-large-boolq-pt-7e-3-nowarmup-03-08-10-58"
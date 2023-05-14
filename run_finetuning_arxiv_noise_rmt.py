import json
import logging
import sys
import os
import math
import random
import shutil
from pathlib import Path
from itertools import chain
from megatron.data.dataset_utils import get_indexed_dataset_

import horovod.torch as hvd
from dotenv import load_dotenv
import torch
import numpy as np
import datasets
from torch.utils.data import DataLoader, DistributedSampler
from datasets import Dataset, load_dataset, load_from_disk
from huggingface_hub import hf_hub_download
from sklearn.metrics import f1_score, accuracy_score

from lm_experiments_tools import TrainerArgs
from lm_experiments_tools.trainer import Trainer

from torch.nn.utils.rnn import pad_sequence
from lm_experiments_tools.lm_datasets import get_lm_datasets
load_dotenv()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# if CUDA_VISIBLE_DEVICES is not set make all gpus visible
if os.environ.get('CUDA_VISIBLE_DEVICES', None) is None:
    os.environ['CUDA_VISIBLE_DEVICES'] = ','.join([str(i) for i in range(torch.cuda.device_count())])

logger.info(f"CUDA_VISIBLE_DEVICES: {os.environ['CUDA_VISIBLE_DEVICES']}")
# first call to torch.cuda.device_count() sets visible gpus, following calls will not change the result
logger.info(f"CUDA DEVICE COUNT: {torch.cuda.device_count()}")

hvd.init()

import transformers  # noqa: E402
from transformers import AutoConfig, AutoTokenizer, HfArgumentParser  # noqa: E402

from lm_experiments_tools.utils import collect_run_configuration, get_cls_by_name, get_optimizer  # noqa: E402
import lm_experiments_tools.optimizers as optimizers  # noqa: E402

# limit # of CPU threads to be used per pytorch worker, otherwise it might use all cpus and throttle gpus
# > 2 fails cause of https://github.com/pytorch/pytorch/issues/56615
# need to upgrade to torch>1.8.1
torch.set_num_threads(4)
# all gpus set with CUDA_VISIBLE_DEVICES are visible to process, indexing from 0 to ...
torch.cuda.set_device(hvd.local_rank())

parser = HfArgumentParser(TrainerArgs)
parser.add_argument('--task_name', type=str, help="Task name, wikitext, ...")
parser.add_argument('--validate_only', action='store_true', default=False,
                    help='Skip training and run only validation. (default: False)')
parser.add_argument('--working_dir', type=str, default='.',
                    help='working dir, should be a dir with t5-experiments repo (default: .)')
parser.add_argument('--seed', type=int, default=42, help='random seed')
parser.add_argument('--show_valid_examples', type=int, default=0,
                    help='how many valid examples to show during training (default: 0)')
parser.add_argument('--input_seq_len', type=int, default=128, help='input sequnce length (default: 128).')
parser.add_argument('--target_seq_len', type=int, default=16, help='target sequnce length, should be set to '
                                                                   'max(len(target))+1 for EOS (default: 16).')
parser.add_argument('--data_n_workers', type=int, default=2, help='number of dataloader workers (default: 2)')

parser.add_argument('--input_prefix', type=str, default='', help='add task prefix to an input string (default: "")')

# model args
parser.add_argument('--from_pretrained', type=str, help='model name in HF Model Hub (default: "")')
parser.add_argument('--model_cfg', type=str, help='path to model configuration file (default: "")')
parser.add_argument('--model_cls', type=str, default='transformers:BertForPreTraining',
                    help='model class name to use (default: transformers:BertForPreTraining)')
parser.add_argument('--model_cpt', type=str, default=None, help='pretrained model checkpoint path')
parser.add_argument('--backbone_cls', type=str, default=None,
                    help='backbone class name to use for RMT')
parser.add_argument('--model_type', type=str, default='encoder-decoder',
                    help='model type, encoder, encoder-decoder, decoder, affects preprocessing '
                         '(default: encoder-decoder)')


# Aydar # RMT args 
parser.add_argument('--input_size', type=int, default=None, help='maximal input size of the backbone model')
parser.add_argument('--num_mem_tokens', type=int, default=None, help='number of memory tokens.')
parser.add_argument('--xl_cache_size', type=int, default=None, help='size of Transformer-XL -like cache')
parser.add_argument('--max_n_segments', type=int, default=1, help='maximal segment number')
parser.add_argument('--noise_n_segments', type=int, default=1, help='number of noisy segments in history')
parser.add_argument('--sum_loss', action='store_true', default=False,
                    help='with this flag task loss from all segments is summed')
parser.add_argument('--bptt_depth', type=int, default=-1, help='max number of previous segments in gradient computation.')
parser.add_argument('--segment_ordering', type=str, help='segment order', default='regular',
                    choices=['regular', 'reversed', 'bidirectional', 'repeat_first', 'last_memory_only'])
parser.add_argument('--memory_forward_func', type=str, help='path to memory forward funсtion script', default=None)
parser.add_argument('--memory_layers', type=str, help='memory-augmented layer inds or "all" for all layers', default=None)
parser.add_argument('--share_memory_layers', action='store_true', help='share weights of memory layers', default=False)
parser.add_argument('--reconstruction_loss_coef', type=float, default=None,
                    help='reconstuction loss ratio in total loss')
# parser.add_argument('--segment_ordering', type=str,help='????', default='regular',
#                     choices=['regular', 'reversed', 'bidirectional', 'repeat_first', 'last_memory_only'])
parser.add_argument('--retain_graph', action='store_true', help='Retain computation graph during backward pass', default=False)
parser.add_argument('--use_truncated_backward', action='store_true', default=False,
                    help='whether to use RMT truncated bptt method in backward')
parser.add_argument('--k1', type=int, default=-1, help='(not implemented) If not -1, gradient update is done each k1 segments')
parser.add_argument('--k2', type=int, default=-1, help='number of last segments used by backward')
parser.add_argument('--freeze_model_weights', action='store_true', default=False,
                    help='Stop training all model weights except memory layers')
parser.add_argument('--backbone_cpt', type=str, default=None, help='backbone model checkpoint path')


# tokenizer
# todo: add wordpiece tokenizers support?
parser.add_argument('--tokenizer', type=str, default=None, help='path or name of pre-trained HF Tokenizer')

# optimizer args
parser.add_argument('--optimizer', type=str, default='AdamW', help='optimizer name: AdamW, Adafactor. (default: AdamW)')
parser.add_argument('--weight_decay', type=float, default=0.0, help='optimizer weight decay (default: 0.0)')
parser.add_argument('--scale_parameter', action='store_true', default=False,
                    help='Adafactor scale_parameter (default: False)')
parser.add_argument('--relative_step', action='store_true', default=False,
                    help='Adafactor relative_step (default: False)')
parser.add_argument('--warmup_init', action='store_true', default=False,
                    help='Adafactor warmup_init (default: False)')



if __name__ == '__main__':
    args = parser.parse_args()
    # set current working dir
    args.working_dir = str(Path(args.working_dir).expanduser().absolute())
    os.chdir(args.working_dir)
    if hvd.rank() == 0:
        logger.info(f'hvd size: {hvd.size()}')
        logger.info(f'FP16: {args.fp16}')

    if hvd.rank() == 0 and args.model_path is None:
        logger.warning('model_path is not set: config, logs and checkpoints will not be saved.')

    # create model path and save configuration
    if hvd.rank() == 0 and args.model_path is not None:
        model_path = Path(args.model_path)
        if not model_path.exists():
            Path(model_path).mkdir(parents=True)
        args_dict = collect_run_configuration(args)
        # todo: if model path exists and there is config file, write new config file aside
        json.dump(args_dict, open(model_path/'config.json', 'w'), indent=4)

    if not args.from_pretrained:
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    else:
        tokenizer = AutoTokenizer.from_pretrained(args.from_pretrained)

    # Prepare datasets
    if hvd.rank() == 0:
        logger.info(f'preparing dataset for {args.task_name}')

    block_size = args.input_size 
    if args.num_mem_tokens is not None:
        block_size -= 2 * args.num_mem_tokens
    if args.xl_cache_size is not None:
        block_size -= args.xl_cache_size
    history_size = args.input_seq_len - block_size
    history_n_segments = args.max_n_segments - 1 - args.noise_n_segments

    # noise dataset

    raw_noise_dataset = load_dataset('wikitext', 'wikitext-2-v1')

    column_names = raw_noise_dataset["train"].column_names
    text_column_name = "text" if "text" in column_names else column_names[0]

    def tokenize_function(examples):
        return tokenizer(examples[text_column_name])

    noise_dataset = raw_noise_dataset.map(
        tokenize_function,
        batched=True,
        remove_columns=column_names,
        desc="Running tokenizer on dataset",
    )

    def group_texts(examples, block_size, history_size=None):
        concatenated_examples = {k: list(chain(*examples[k])) for k in examples.keys()}
        total_length = len(concatenated_examples[list(examples.keys())[0]])

        if history_size is None:
            result = {
                k: [t[i : i + block_size] for i in range(0, total_length-block_size, block_size)]
                for k, t in concatenated_examples.items()
            }
        else:
            result = {
                k: [t[max({0, i - history_size}) : i + block_size] for i in range(0, total_length-block_size, block_size)]
                for k, t in concatenated_examples.items()
            }
        result["labels"] = result["input_ids"].copy()
        return result

    # id_pad_value = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    # def collate_fn_noise(batch):
    #     input_ids = [torch.tensor(b['input_ids'][::-1]) for b in batch]
    #     labels = [torch.tensor(b['labels'][::-1]) for b in batch]
    #     attention_mask = [torch.tensor(b['attention_mask'][::-1]) for b in batch]
    #     input_ids = pad_sequence(input_ids, padding_value=id_pad_value).T.flip(1)
    #     labels = pad_sequence(labels, padding_value=-100).T.flip(1)
    #     attention_mask = pad_sequence(attention_mask, padding_value=0).T.flip(1)

    #     collated = {'input_ids': input_ids,
    #                 'labels': labels, 
    #                 'attention_mask': attention_mask}
        
    #     if input_ids.shape[1] != block_size:
    #         labels_mask = torch.ones_like(input_ids, dtype=bool)
    #         labels_mask[:, :-block_size] = False
    #         collated['labels_mask'] = labels_mask
        
    #     print('\n\n\nnoise')
    #     for key in collated:
    #         print(key, collated[key].shape)
    #     return collated


    train_noise_dataset = noise_dataset["train"].map(lambda x: group_texts(x, block_size), 
                                            batched=True, desc=f"Grouping train noise in chunks of {block_size}")
    valid_noise_dataset = noise_dataset["validation"].map(lambda x: group_texts(x, block_size), 
                                            batched=True, desc=f"Grouping valid noise in chunks of {block_size}")


    class segmentDataLoaderOTF_noise(DataLoader):
        def __init__(self, dataset, noise_dataset, block_size, history_n_segments, noise_n_segments, max_samples=None, shuffle=False, *args, **kwargs):
            super().__init__(dataset, *args, **kwargs)
            self.block_size = block_size
            self.history_n_segments = history_n_segments
            self.noise_n_segments = noise_n_segments
            self.max_samples = max_samples
            self.shuffle = shuffle
            self.noise_dataset = noise_dataset
        
        def get_samples(self, document):
            input_ids, attention_mask = document['input_ids'], document['attention_mask']
            samples = [input_ids[start: start + self.block_size] for start in range(0, len(input_ids), self.block_size)]
            samples = [samples[max({0, i - self.history_n_segments - 1}):i] for i in range(1, len(samples))]

            for sample in samples:
                noise_positions = np.random.choice(range(len(sample)), self.noise_n_segments)
                # print('noise_positions', noise_positions)
                for p in noise_positions:
                    noise_sample = self.noise_dataset[np.random.randint(len(self.noise_dataset))]['input_ids']
                    sample.insert(p, noise_sample)

            return samples
        
        def __iter__(self):
            inds = list(range(len(self.dataset)))

            if self.max_samples is not None:
                inds = inds[:self.max_samples]

            if self.shuffle: 
                random.shuffle(inds)

            doc_ind = 0
            samples = []

            while True:
                if doc_ind >= len(inds):
                    break
                try: 
                    while len(samples) < self.batch_size:
                        document = self.dataset[inds[doc_ind]]
                        doc_ind += 1
                        samples += self.get_samples(document)

                        if doc_ind >= len(inds):
                            raise(StopIteration)
                except(StopIteration):
                    pass

                batch, samples = samples[:self.batch_size], samples[self.batch_size:]
                yield self.collate_fn(batch)

    id_pad_value = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    def collate_fn(batch):
        input_ids = labels = [torch.tensor(list(chain(*b))[::-1]) for b in batch]
        attention_mask = [torch.ones_like(b, dtype=int) for b in input_ids]
        input_ids = pad_sequence(input_ids, padding_value=id_pad_value).T.flip(1)
        labels = pad_sequence(labels, padding_value=-100).T.flip(1)
        attention_mask = pad_sequence(attention_mask, padding_value=0).T.flip(1)

        collated = {'input_ids': input_ids,
                    'labels': labels, 
                    'attention_mask': attention_mask}

        if input_ids.shape[1] != block_size:
            labels_mask = torch.ones_like(input_ids, dtype=bool)
            labels_mask[:, :-block_size] = False
            collated['labels_mask'] = labels_mask

        # print('\n\n\ndata')
        # for key in collated:
        #     print(key, collated[key].shape)
        
        # if collated['labels_mask'].shape[1] != 472:
        #     print('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\nlen(batch), [len(b) for b in batch]', len(batch), [len(b) for b in batch])
        # else:
        #     print('len(batch), [len(b) for b in batch]', len(batch), [len(b) for b in batch])

        
        return collated


    train_dataset = load_from_disk('/cephfs/home/bulatov/bulatov/datasets/arxiv_pile/tokenized/train')
    valid_dataset = load_from_disk('/cephfs/home/bulatov/bulatov/datasets/arxiv_pile/tokenized/valid')
    test_dataset = load_from_disk('/cephfs/home/bulatov/bulatov/datasets/arxiv_pile/tokenized/test')

    
    # shuffle train data each epoch (one loop over train_dataset)
    train_sampler = DistributedSampler(train_dataset, rank=hvd.rank(), num_replicas=hvd.size(), shuffle=True,
                                       drop_last=False, seed=args.seed)
    per_worker_batch_size = args.batch_size * args.gradient_accumulation_steps
    global_batch_size = per_worker_batch_size * hvd.size()
    kwargs = {'pin_memory': True, 'num_workers': args.data_n_workers}
    if not args.validate_only:
        train_dataloader = segmentDataLoaderOTF_noise(train_dataset, 
                                        batch_size=per_worker_batch_size, 
                                        noise_dataset=train_noise_dataset,
                                        sampler=train_sampler,
                                        block_size=block_size, 
                                        history_n_segments=history_n_segments, 
                                        noise_n_segments=args.noise_n_segments,
                                        shuffle=True,
                                        collate_fn=collate_fn, **kwargs)
    else:
        train_dataloader = None

    # get validation dataset
    # max_samples = 1000 if args.validate_only else 100
    max_samples = 100
    valid_dataloader = None
    if hvd.rank() == 0:
        logger.info(f'preparing validation data')
    valid_sampler = DistributedSampler(valid_dataset, rank=hvd.rank(), num_replicas=hvd.size(), shuffle=False)
    valid_dataloader = segmentDataLoaderOTF_noise(valid_dataset, batch_size=per_worker_batch_size, sampler=valid_sampler,
                                    noise_dataset=valid_noise_dataset,
                                    block_size=block_size, 
                                    history_n_segments=history_n_segments, 
                                    noise_n_segments=args.noise_n_segments,
                                    shuffle=False,
                                    max_samples=max_samples,
                                    collate_fn=collate_fn, drop_last=True, **kwargs)
    
    # # get test dataset
    # test_sampler = DistributedSampler(test_dataset, rank=hvd.rank(), num_replicas=hvd.size(), shuffle=False)
    # test_dataloader = segmentDataLoaderOTF(test_dataset, batch_size=per_worker_batch_size, sampler=test_sampler,
    #                                 block_size=block_size, 
    #                                 history_size=history_size, 
    #                                 shuffle=False,
    #                                 max_samples=max_samples,
    #                                 collate_fn=collate_fn, drop_last=True, **kwargs)
    
    if args.valid_interval is None:
        args.valid_interval = args.log_interval

    # define model
    model_cls = get_cls_by_name(args.backbone_cls)
    if hvd.rank() == 0:
        logger.info(f'Using model class: {model_cls}')
    if not args.from_pretrained:
        model_cfg = AutoConfig.from_pretrained(args.model_cfg)
        model = model_cls(config=model_cfg)
    else:
        if hvd.rank() == 0:
            logger.info(f'Loading pretrained model: {args.from_pretrained}')
        model = model_cls.from_pretrained(args.from_pretrained)

    # Aydar # Pass memory settings to pretrained model
    if args.num_mem_tokens is not None:
        if args.memory_forward_func is not None:
            args.memory_forward_func = get_cls_by_name(args.memory_forward_func)

        rmt_config = {
            'num_mem_tokens': args.num_mem_tokens, 
            'max_n_segments': args.max_n_segments,
            'xl_cache_size': args.xl_cache_size,
            # 'segment_ordering': args.segment_ordering,
            'input_size': args.input_size,
            'k1': args.k1, 'k2': args.k2,
            'sum_loss': args.sum_loss,
            'tokenizer': tokenizer,
            'memory_forward_func': args.memory_forward_func,
            'memory_layers': args.memory_layers,
            'share_memory_layers': args.share_memory_layers,
            'reconstruction_loss_coef': args.reconstruction_loss_coef,
        }
        rmt_cls = get_cls_by_name(args.model_cls)
        if hvd.rank() == 0:
            logger.info(f'Wrapping in: {rmt_cls}')
        
        ## load cpt of backbone model
        if args.backbone_cpt:
            backbone_cpt = os.path.join(args.backbone_cpt, "model_best.pth")
            cpt = torch.load(backbone_cpt, map_location='cpu')
            model.load_state_dict(cpt['model_state_dict'])
            if hvd.rank() == 0:
                logger.info(f'Loaded baseline state dict from: {args.backbone_cpt}')
        
        model = rmt_cls(model, **rmt_config)

        ## turn on memory resetting on validation
        model.rmt_config['reinit_mem_each_fwd'] = True

        ## load cpt of rmt
        if args.model_cpt:
            model_cpt = os.path.join(args.model_cpt, "model_best.pth")
            cpt = torch.load(model_cpt, map_location='cpu')
            model.load_state_dict(cpt['model_state_dict'])
            if hvd.rank() == 0:
                logger.info(f'Loaded RMT state dict from: {args.model_cpt}')

        if args.freeze_model_weights:
            for n, p in model.named_parameters():
                # if 'memory' not in n and 'wte' not in n:
                if 'memory' not in n:
                    p.requires_grad = False
            if hvd.rank() == 0:
                logger.info(f'Frozen moodel weights')
                logger.info(f'Remaining parameters: {[n for n, p in model.named_parameters() if p.requires_grad]}')

    
    # define optimizer
    optimizer_cls = get_optimizer(args.optimizer)
    if optimizer_cls is None:
        raise RuntimeError(f'{args.optimizer} was not found in optimizers, torch.optim, transformers.optimization')

    if hvd.rank() == 0:
        logger.info(f'Using optimizer class: {optimizer_cls}')

    # todo: group optimizer params
    if optimizer_cls in [transformers.optimization.Adafactor, optimizers.Adafactor]:
        # https://github.com/huggingface/transformers/pull/9751/files -> transformers 4.3.0
        optimizer = optimizer_cls(model.parameters(), lr=args.lr,
                                  scale_parameter=args.scale_parameter,
                                  relative_step=args.relative_step,
                                  warmup_init=args.warmup_init,
                                  weight_decay=args.weight_decay)
    else:
        optimizer = optimizer_cls(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # for encoder only classification
    def keep_for_metrics_fn(batch, output):
        # select data from batch and model output that would be used to compute metrics
        data = {}
        data['labels'] = batch['labels']
        data['loss'] = output['loss']
        data['predictions'] = torch.argmax(output['logits'].detach(), dim=-1)
        return data

    # HF datasets can compute metrics on each gpu process and then aggregate them on process with rank 0
    # synchronization is done by using temporay files on a shared filesystem
    # rank and number of workers is set by num_process and process_id params
    # BUT our Trainer aggregates all prediction from all gpus!
    #   this will lead to computing metrics for predictions repeated xN_GPUS times
    # need to try:
    # - keep_in_memory=True, may lead to OOM for large validation sets, after sync predictions and targets for the full
    #       validation set would be stored on each GPU -> xN_GPUs RAM
    #   - implemented currently
    # - compute metrics on batch lvl
    # - add support of HF metrics and turn off aggregation in case if metric has .add_batch method
    # scrolls_metric = datasets.load_metric(scrolls_metric_path, args.task_name, keep_in_memory=True)

    def metrics_fn(data):
        # compute metrics based on stored labels, predictions, ...
        metrics = {}
        y, p = data['labels'], data['predictions']
        if hvd.rank() == 0 and args.show_valid_examples > 0:
            for i in range(min(args.show_valid_examples, len(y))):
                # logger.info(f'y: {tokenizer.decode(y[i])}')
                # logger.info(f'p: {tokenizer.decode(p[i])}')
                logger.info(f'y: {y[i]}')
                logger.info(f'p: {p[i]}')
                logger.info('-' * 50)
        try:
            perplexity = math.exp(data["loss"].mean())
        except OverflowError:
            perplexity = float("inf")
        metrics["perplexity"] = perplexity

        return metrics

    ### booydar
    batch_metrics_fn = lambda _, y: {key: y[key] for key in y.keys() if (('loss' in key) or ('!log' in key))}
    trainer = Trainer(args, model, optimizer, train_dataloader, valid_dataloader, train_sampler,
                      keep_for_metrics_fn=keep_for_metrics_fn, metrics_fn=metrics_fn,
                      ###booydar
                      batch_metrics_fn=batch_metrics_fn,
                      generate_kwargs={})

    if not args.validate_only:
        # train loop
        trainer.train()
        # make sure all workers are done
        hvd.barrier()
        # run validation after training
        if args.save_best:
            best_model_path = str(Path(args.model_path) / 'model_best.pth')
            if hvd.rank() == 0:
                logger.info(f'Loading best saved model from {best_model_path}')
            trainer.load(best_model_path)
        if valid_dataloader is not None:
            if hvd.rank() == 0:
                logger.info('Runnning validation on valid data:')
            trainer.validate(valid_dataloader, write_tb=False)
        # if test_dataloader is not None:
        #     if hvd.rank() == 0:
        #         logger.info('Runnning validation on test data:')
        #     trainer.validate(test_dataloader, write_tb=False)
    else:
        # run validation, do not write to tensorboard
        # if hvd.rank() == 0:
        #     logger.info('Running validation on train set:')
        # trainer.validate(train_dataloader, split='train', write_tb=True)
        if valid_dataloader is not None:
            if hvd.rank() == 0:
                logger.info('Running validation on valid data:')
            trainer.validate(valid_dataloader, write_tb=True)
        # if test_dataloader is not None:
        #     if hvd.rank() == 0:
        #         logger.info('Runnning validation on test data:')
        #     trainer.validate(test_dataloader, split='test', write_tb=True)

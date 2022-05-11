from collections import defaultdict
import shutil
import sys
import os

curPath = os.path.abspath(os.path.dirname(__file__))
rootPath = os.path.split(curPath)[0]
sys.path.append(rootPath)

from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm, trange

import argparse
import logging
import random
import numpy as np
import torch
import torch.nn as nn

from torch.nn import CrossEntropyLoss, MSELoss, BCEWithLogitsLoss

from pytorch_transformers import BertModel, BasicTokenizer
from pytorch_transformers import AdamW, WarmupLinearSchedule

from torch.utils.data import (
    DataLoader,
    RandomSampler,
    SequentialSampler,
    TensorDataset,
    DistributedSampler,
)

from pytorch_transformers.tokenization_bert import BertTokenizer as BertTokenizerLocal
from transformers import BertTokenizer as BertTokenizerHugging

from utils_sec import (
    output_modes,
    processors,
    convert_examples_to_features_sec,
    SECDataset,
)

logger = logging.getLogger(__name__)


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


class RNNModel(nn.Module):
    def __init__(self, args):
        super(RNNModel, self).__init__()
        self.args = args
        self.input_size = args.rnn_input_size
        self.hidden_size = args.rnn_hidden_size
        self.num_layers = args.rnn_num_layers
        self.num_classes = args.rnn_num_classes

        self.lstm = nn.LSTM(
            self.input_size, self.hidden_size, self.num_layers, batch_first=True
        )
        self.linear_layers = nn.Sequential(
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.Dropout(args.rnn_dropout_prob),
            nn.ReLU(),
            nn.Linear(self.hidden_size, 64),
            nn.Dropout(args.rnn_dropout_prob),
            nn.ReLU(),
            nn.Linear(64, self.num_classes),
        )

    def forward(self, x):
        # Set initial hidden and cell states
        h0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(
            self.args.device
        )
        c0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(
            self.args.device
        )

        # Forward propagate LSTM
        out, _ = self.lstm(
            x, (h0, c0)
        )  # out: tensor of shape (batch_size, seq_length, hidden_size)

        # Decode the hidden state of the last time step
        out = self.linear_layers(out[:, -1, :])
        return out

    def save_pretrained(self, save_directory):
        assert os.path.isdir(
            save_directory
        ), "Saving path should be a directory where the model can be saved"
        # Only save the model it-self if we are using distributed training
        model_to_save = self.module if hasattr(self, "module") else self
        # If we save using the predefined names, we can load using `from_pretrained`
        output_model_file = os.path.join(save_directory, "rnn_pytorch_model.bin")
        torch.save(model_to_save.state_dict(), output_model_file)
        logger.info("Saving model checkpoint to %s", save_directory)


class KPIModel(nn.Module):
    def __init__(self, args):
        super(KPIModel, self).__init__()
        self.input_size = args.kpi_input_size
        self.num_classes = args.kpi_num_classes
        self.hidden_size = args.kpi_hidden_size
        self.hidden_layers = args.kpi_hidden_layers

        if self.hidden_layers == 0:
            self.layers = nn.Linear(self.input_size, self.num_classes)
        elif self.hidden_layers == 1:
            self.layers = nn.Sequential(
                nn.Linear(self.input_size, self.hidden_size),
                nn.Dropout(args.kpi_dropout_prob),
                nn.ReLU(),
                nn.Linear(self.hidden_size, self.num_classes),
            )

    def forward(self, x, labels=None):
        outputs = self.layers(x)

        if labels is not None:
            loss_fct = MSELoss()
            loss = loss_fct(outputs.squeeze(1), labels)
            outputs = (loss, outputs)

        return outputs

    def save_pretrained(self, save_directory):
        assert os.path.isdir(
            save_directory
        ), "Saving path should be a directory where the model can be saved"
        # Only save the model it-self if we are using distributed training
        model_to_save = self.module if hasattr(self, "module") else self
        # If we save using the predefined names, we can load using `from_pretrained`
        output_model_file = os.path.join(save_directory, "kpi_pytorch_model.bin")
        torch.save(model_to_save.state_dict(), output_model_file)
        logger.info("Saving model checkpoint to %s", save_directory)


class PretrainedModel(nn.Module):
    def __init__(self, args):
        super(PretrainedModel, self).__init__()
        self.model = BertModel.from_pretrained(
            pretrained_model_name_or_path=args.finbert_path, output_hidden_states=True
        )
        self.config = self.model.config
        self.config.freeze_adapter = args.freeze_adapter
        if args.freeze_bert:
            for p in self.parameters():
                p.requires_grad = False

    def forward(
        self,
        input_ids,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        labels=None,
        start_id=None,
    ):
        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
        )

        return outputs  # (loss), logits, (hidden_states), (attentions)


class AdapterEnsembleModel(nn.Module):
    def __init__(self, args, pretrained_model_config, sec_adapter):
        super(AdapterEnsembleModel, self).__init__()
        self.args = args
        self.config = pretrained_model_config

        # self.adapter = AdapterModel(self.args, pretrained_model_config)
        self.sec_adapter = sec_adapter

        if args.freeze_adapter and (self.sec_adapter is not None):
            for p in self.sec_adapter.parameters():
                p.requires_grad = False

        if self.args.fusion_mode == "concat":
            # self.task_dense_lin = nn.Linear(self.config.hidden_size + self.config.hidden_size, self.config.hidden_size)
            self.task_dense_sec = nn.Linear(
                self.config.hidden_size + self.config.hidden_size,
                self.config.hidden_size,
            )
            # self.task_dense = nn.Linear(self.config.hidden_size + self.config.hidden_size, self.config.hidden_size)

        """
            These might be in the RNN
        """
        # self.num_labels = args.rnn_num_classes
        # self.dense = nn.Linear(self.config.hidden_size, self.config.hidden_size)
        # self.dropout = nn.Dropout(self.config.hidden_dropout_prob)
        # self.out_proj = nn.Linear(self.config.hidden_size, self.num_labels)

    def forward(
        self,
        pretrained_model_outputs,
        input_ids,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        labels=None,
        start_id=None,
    ):
        pretrained_model_last_hidden_states = pretrained_model_outputs[0]
        if self.sec_adapter is not None:
            sec_adapter_outputs, _ = self.sec_adapter(pretrained_model_outputs)

        # if self.args.fusion_mode == "add":
        #     task_features = pretrained_model_last_hidden_states
        #     if self.sec_adapter is not None:
        #         task_features = task_features + sec_adapter_outputs
        # elif self.args.fusion_mode == "concat":
        #     combine_features = pretrained_model_last_hidden_states
        #     sec_features = self.task_dense_sec(
        #         torch.cat([combine_features, sec_adapter_outputs], dim=2)
        #     )

        # Just for testing | DELETE WHEN Adapter is here
        sec_features = pretrained_model_last_hidden_states

        sec_features_squeezed = sec_features[:, 0, :]
        # sec_features_squeezed = sec_features_1.squeeze(dim=1)

        """
            New logic using RNN, return the encoded representation
        """
        return sec_features_squeezed

        """
            Old logic for outputing directly from BERT
        """
        # logits = self.out_proj(self.dropout(self.dense(sec_features_squeezed)))

        # outputs = (logits,) + pretrained_model_outputs[2:]
        # if labels is not None:
        #     if self.num_labels == 1:
        #         #  We are doing regression
        #         loss_fct = MSELoss()
        #         loss = loss_fct(logits, labels.unsqueeze(1))
        #     else:
        #         # loss_fct = CrossEntropyLoss()
        #         loss_fct = BCEWithLogitsLoss()
        #         loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1, self.num_labels))
        #     outputs = (loss,) + outputs

        # return outputs  # (loss), logits, (hidden_states), (attentions)

    def save_pretrained(self, save_directory):
        assert os.path.isdir(
            save_directory
        ), "Saving path should be a directory where the model and configuration can be saved"
        # Only save the model it-self if we are using distributed training
        model_to_save = self.module if hasattr(self, "module") else self
        # Save configuration file
        model_to_save.config.save_pretrained(save_directory)
        # If we save using the predefined names, we can load using `from_pretrained`
        output_model_file = os.path.join(
            save_directory, "adapter_ensemble_pytorch_model.bin"
        )
        torch.save(model_to_save.state_dict(), output_model_file)
        logger.info("Saving model checkpoint to %s", save_directory)


def load_and_cache_examples(args, task, tokenizer, dataset_type, evaluate=False):
    # Modify for our dataset
    # dataset_type: train, dev, test
    if args.local_rank not in [-1, 0] and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    processor = processors[task]()
    output_mode = output_modes[task]
    # Load data features from cache or dataset file
    cached_features_file = os.path.join(
        args.data_dir,
        "cached_{}_{}_{}_{}_{}_{}".format(
            dataset_type,
            list(filter(None, args.finbert_path.split("/"))).pop(),
            str(args.max_seq_length),
            str(task),
            str(args.percentage_change_type),
            str(args.type_text),
        ),
    )
    # Remove "not" when finished with development
    if os.path.exists(cached_features_file):
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        examples = (
            processor.get_dev_examples(
                args.data_dir, args.percentage_change_type, args.type_text, dataset_type
            )
            if evaluate
            else processor.get_train_examples(
                args.data_dir, args.percentage_change_type, args.type_text, dataset_type
            )
        )
        features = convert_examples_to_features_sec(
            examples,
            args.max_seq_length,
            tokenizer,
            output_mode,
            cls_token=tokenizer.cls_token,
            sep_token=tokenizer.sep_token,
            pad_on_left=False,
            pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[0],
            pad_token_segment_id=0,
        )
        if args.local_rank in [-1, 0]:
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save(features, cached_features_file)

    if args.local_rank == 0 and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Convert to Tensors and build dataset

    # Convert input ids, segment_ids and input masks first
    for filing_feature in features:
        for idx, paragraph_feature in enumerate(
            filing_feature.list_input_features_paragraphs
        ):
            filing_feature.list_input_features_paragraphs[idx].input_ids = torch.tensor(
                paragraph_feature.input_ids, dtype=torch.long
            )
            filing_feature.list_input_features_paragraphs[
                idx
            ].input_mask = torch.tensor(paragraph_feature.input_mask, dtype=torch.long)
            filing_feature.list_input_features_paragraphs[
                idx
            ].segment_ids = torch.tensor(
                paragraph_feature.segment_ids, dtype=torch.long
            )
        filing_feature.list_numerical_kpi_features = torch.tensor(
            filing_feature.list_numerical_kpi_features, dtype=torch.float
        )

    # Process numerical KPI features
    # kpi_features = torch.tensor([f.list_numerical_kpi_features for f in features], dtype=torch.float)
    if output_mode == "classification":
        all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.float)
    elif output_mode == "regression":
        all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.float)

    dataset = SECDataset(features, all_label_ids, args.max_seq_length)

    return dataset


def train(args, train_dataset, val_dataset, model, tokenizer):
    """Train the model"""
    pretrained_finbert_model = model[0]
    adapter_ensemble_model = model[1]
    rnn_model = model[2]
    kpi_model = model[3]

    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter(log_dir="runs/" + args.my_model_name)

    train_sampler = (
        RandomSampler(train_dataset)
        if args.local_rank == -1
        else DistributedSampler(train_dataset)
    )
    train_dataloader = DataLoader(
        train_dataset,
        sampler=train_sampler,
        batch_size=args.train_batch_size // args.gradient_accumulation_steps,
    )

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = (
            args.max_steps
            // (len(train_dataloader) // args.gradient_accumulation_steps)
            + 1
        )
    else:
        t_total = (
            len(train_dataloader)
            // args.gradient_accumulation_steps
            * args.num_train_epochs
        )

    # Prepare optimizer and schedule (linear warmup and decay)

    """
        Optimizer and Scheduler for BERT models
    """
    no_decay = ["bias", "LayerNorm.weight"]
    # This lets us combine parameters which we want to change using the optimizer. Can be from couple of models
    # Use these grouped parameters only if we want to touch the BERT weights as well
    # if args.freeze_bert:
    #     optimizer_grouped_parameters = [
    #         # Adapter Ensemble Bert model parameters
    #         {
    #             "params": [
    #                 p
    #                 for n, p in adapter_ensemble_model.named_parameters()
    #                 if not any(nd in n for nd in no_decay)
    #             ],
    #             "weight_decay": args.weight_decay,
    #         },
    #         {
    #             "params": [
    #                 p
    #                 for n, p in adapter_ensemble_model.named_parameters()
    #                 if any(nd in n for nd in no_decay)
    #             ],
    #             "weight_decay": 0.0,
    #         },
    #         # RNN Model parameters
    #         {
    #             "params": [
    #                 p
    #                 for n, p in rnn_model.named_parameters()
    #                 if not any(nd in n for nd in no_decay)
    #             ],
    #             "weight_decay": args.weight_decay,
    #         },
    #         {
    #             "params": [
    #                 p
    #                 for n, p in rnn_model.named_parameters()
    #                 if any(nd in n for nd in no_decay)
    #             ],
    #             "weight_decay": 0.0,
    #         },
    #     ]
    # else:
    # optimizer_grouped_parameters = [
    #     {
    #         "params": [
    #             p
    #             for n, p in adapter_ensemble_model.named_parameters()
    #             if not any(nd in n for nd in no_decay)
    #         ],
    #         "weight_decay": args.weight_decay,
    #     },
    #     {
    #         "params": [
    #             p
    #             for n, p in adapter_ensemble_model.named_parameters()
    #             if any(nd in n for nd in no_decay)
    #         ],
    #         "weight_decay": 0.0,
    #     },
    #     {
    #         "params": [
    #             p
    #             for n, p in pretrained_finbert_model.named_parameters()
    #             if not any(nd in n for nd in no_decay)
    #         ],
    #         "weight_decay": args.weight_decay,
    #     },
    #     {
    #         "params": [
    #             p
    #             for n, p in pretrained_finbert_model.named_parameters()
    #             if any(nd in n for nd in no_decay)
    #         ],
    #         "weight_decay": 0.0,
    #     },
    #     {
    #         "params": [
    #             p
    #             for n, p in rnn_model.named_parameters()
    #             if not any(nd in n for nd in no_decay)
    #         ],
    #         "weight_decay": args.weight_decay,
    #     },
    #     {
    #         "params": [
    #             p
    #             for n, p in rnn_model.named_parameters()
    #             if any(nd in n for nd in no_decay)
    #         ],
    #         "weight_decay": 0.0,
    #     },
    # ]

    # Comment out if using the real grouped parameters
    optimizer_grouped_parameters = rnn_model.parameters()
    optimizer = AdamW(
        optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon
    )
    scheduler = WarmupLinearSchedule(
        optimizer, warmup_steps=args.warmup_steps, t_total=t_total
    )

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Total train batch size = %d", args.train_batch_size)
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    logger.info("Try resume from checkpoint")
    if args.restore:
        if os.path.exists(os.path.join(args.output_dir, "global_step.bin")):
            logger.info("Load last checkpoint data")
            global_step = torch.load(os.path.join(args.output_dir, "global_step.bin"))
            global_step += 1
            start_epoch = int(global_step / len(train_dataloader)) - 1
            output_dir = os.path.join(
                args.output_dir, "checkpoint-{}".format(start_epoch)
            )
            logger.info("Load from output_dir {}".format(output_dir))

            optimizer.load_state_dict(
                torch.load(os.path.join(output_dir, "optimizer.bin"))
            )
            scheduler.load_state_dict(
                torch.load(os.path.join(output_dir, "scheduler.bin"))
            )
            adapter_ensemble_model.load_state_dict(
                torch.load(
                    os.path.join(output_dir, "adapter_ensemble_pytorch_model.bin")
                )
            )
            rnn_model.load_state_dict(
                torch.load(os.path.join(output_dir, "rnn_pytorch_model.bin"))
            )

            # global_step += 1
            # start_epoch = int(global_step / len(train_dataloader))
            # Load the epoch that ended and continue from the next one
            start_epoch += 1
            start_step = global_step - start_epoch * len(train_dataloader) - 1
            logger.info(
                "Start from global_step={} epoch={}".format(global_step, start_epoch)
            )
            if args.local_rank in [-1, 0]:
                tb_writer = SummaryWriter(
                    log_dir="runs/" + args.my_model_name, purge_step=global_step
                )

        else:
            global_step = 0
            start_epoch = 0
            start_step = 0
            if args.local_rank in [-1, 0]:
                tb_writer = SummaryWriter(
                    log_dir="runs/" + args.my_model_name, purge_step=global_step
                )

            logger.info("Start from scratch")
    else:
        global_step = 0
        start_epoch = 0
        start_step = 0
        if args.local_rank in [-1, 0]:
            tb_writer = SummaryWriter(
                log_dir="runs/" + args.my_model_name, purge_step=global_step
            )
            pass
        logger.info("Start from scratch")

    tr_loss, logging_loss = 0.0, 0.0
    pretrained_finbert_model.zero_grad()
    adapter_ensemble_model.zero_grad()
    rnn_model.zero_grad()

    train_iterator = trange(
        (start_epoch, int(args.num_train_epochs)),
        desc="Epoch",
        disable=args.local_rank not in [-1, 0],
    )
    set_seed(args)  # Added here for reproductibility (even between python 2 and 3)
    loss_fct = MSELoss()
    for epoch_step in train_iterator:
        epoch_iterator = tqdm(
            train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0]
        )
        epoch_loss = 0
        for step, batch in enumerate(epoch_iterator):
            # if args.freeze_bert:
            #     pretrained_finbert_model.eval()
            # else:
            #     pretrained_finbert_model.train()
            # adapter_ensemble_model.train()
            rnn_model.train()

            curr_batch_input_ids = defaultdict(list)
            curr_batch_input_masks = defaultdict(list)
            curr_batch_segment_ids = defaultdict(list)

            # Organize the input data for the whole batch in a better way
            for item_paragraph in batch[0]:
                for idx in range(item_paragraph["input_ids"].shape[0]):
                    curr_batch_input_ids[idx].append(item_paragraph["input_ids"][idx])
                    curr_batch_input_masks[idx].append(
                        item_paragraph["input_mask"][idx]
                    )
                    curr_batch_segment_ids[idx].append(
                        item_paragraph["segment_ids"][idx]
                    )

            curr_batch_outputs_from_rnn = []
            # Process curr batch of filings from the batch
            for my_batch in range(len(curr_batch_input_ids)):
                curr_filing_input_ids = curr_batch_input_ids[my_batch]
                curr_filing_input_masks = curr_batch_input_masks[my_batch]
                curr_filing_segment_ids = curr_batch_segment_ids[my_batch]

                curr_filing_encoded_paragraphs = []
                for input_ids, input_masks, segment_ids in zip(
                    curr_filing_input_ids,
                    curr_filing_input_masks,
                    curr_filing_segment_ids,
                ):
                    # Check if all paragraphs are processed
                    if (
                        torch.count_nonzero(input_ids) == 0
                        and torch.count_nonzero(input_masks) == 0
                        and torch.count_nonzero(segment_ids) == 0
                    ):
                        break

                    # batch = tuple(t.to(args.device) for t in batch)
                    input_curr_paragraph = {
                        "input_ids": input_ids[None, :].to(args.device),
                        "attention_mask": input_masks[None, :].to(args.device),
                        "token_type_ids": segment_ids[None, :].to(args.device),
                    }

                    pretrained_model_outputs = pretrained_finbert_model(
                        **input_curr_paragraph
                    )
                    encoded_paragraph = adapter_ensemble_model(
                        pretrained_model_outputs, **input_curr_paragraph
                    )
                    curr_filing_encoded_paragraphs.append(encoded_paragraph.squeeze(0))

                """
                    Use the RNN and generate the loss for this filing
                """

                # Convert list to tensor
                curr_filing_encoded_paragraphs = torch.stack(
                    curr_filing_encoded_paragraphs
                )
                curr_filing_encoded_paragraphs = (
                    curr_filing_encoded_paragraphs.unsqueeze(0).to(args.device)
                )

                rnn_output_for_filing = rnn_model(curr_filing_encoded_paragraphs)
                curr_batch_outputs_from_rnn.append(rnn_output_for_filing)

            # Convert list to tensor for RNN outputs
            curr_batch_outputs_from_rnn = torch.stack(curr_batch_outputs_from_rnn)
            curr_batch_outputs_from_rnn = curr_batch_outputs_from_rnn.squeeze(1)
            curr_batch_labels = batch[2].to(args.device).unsqueeze(1)

            # Run the through the KPI model
            with torch.no_grad():
                kpi_outputs = kpi_model(batch[1].to(args.device), curr_batch_labels)
                kpi_loss = kpi_outputs[0]

            # TODO: Combine the loss in the loss function, write a custom loss fct most probably

            loss = loss_fct(curr_batch_outputs_from_rnn, curr_batch_labels)
            epoch_iterator.set_description("loss {}".format(loss))
            loss.backward()

            """Clipping gradients"""
            # torch.nn.utils.clip_grad_norm_(
            #     pretrained_finbert_model.parameters(), args.max_grad_norm
            # )
            # torch.nn.utils.clip_grad_norm_(
            #     adapter_ensemble_model.parameters(), args.max_grad_norm
            # )
            # torch.nn.utils.clip_grad_norm_(rnn_model.parameters(), args.max_grad_norm)

            tr_loss += loss.item()
            epoch_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                optimizer.step()
                scheduler.step()  # Update learning rate schedule

                # pretrained_finbert_model.zero_grad()
                adapter_ensemble_model.zero_grad()
                rnn_model.zero_grad()
                global_step += 1

        # Epoch ended
        # Log metrics training
        tb_writer.add_scalar("lr", scheduler.get_lr()[0], epoch_step)
        tb_writer.add_scalar("loss", epoch_loss / step, epoch_step)
        # Log metrics evaluation
        results = evaluate(args, val_dataset, model)
        for key, value in results.items():
            tb_writer.add_scalar("eval_{}".format(key), value, epoch_step)
        if (
            args.local_rank in [-1, 0]
            and args.save_epoch_steps > 0
            and epoch_step % args.save_epoch_steps == 0
        ):
            # Save model checkpoint
            output_dir = os.path.join(
                args.output_dir, "checkpoint-{}".format(epoch_step)
            )
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            rnn_model.save_pretrained(
                output_dir
            )  # save to pytorch_model.bin  model.state_dict()
            adapter_ensemble_model.save_pretrained(
                output_dir
            )  # save to pytorch_model.bin  model.state_dict()

            torch.save(
                optimizer.state_dict(),
                os.path.join(output_dir, "optimizer.bin"),
            )
            torch.save(
                scheduler.state_dict(),
                os.path.join(output_dir, "scheduler.bin"),
            )
            torch.save(args, os.path.join(output_dir, "training_args.bin"))
            torch.save(global_step, os.path.join(args.output_dir, "global_step.bin"))

            logger.info(
                "Saving model checkpoint, optimizer, global_step to %s",
                output_dir,
            )
            if (epoch_step / args.save_epoch_steps) > args.max_save_checkpoints:
                try:
                    shutil.rmtree(
                        os.path.join(
                            args.output_dir,
                            "checkpoint-{}".format(
                                epoch_step
                                - args.max_save_checkpoints * args.save_epoch_steps
                            ),
                        )
                    )
                except OSError as e:
                    print(e)

        if args.max_steps > 0 and global_step > args.max_steps:
            epoch_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


save_results = []


def evaluate(args, eval_dataset, model, prefix=""):
    loss_fct = MSELoss()
    pretrained_finbert_model = model[0]
    adapter_ensemble_model = model[1]
    rnn_model = model[2]
    kpi_model = model[3]

    results = {}

    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(args.output_dir)

    # args.eval_batch_size = args.eval_batch_size * max(1, args.n_gpu)
    # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(
        eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size
    )

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Validation Batch size = %d", args.eval_batch_size)
    eval_loss = 0.0
    nb_eval_steps = 0
    preds = None
    out_label_ids = None

    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        adapter_ensemble_model.eval()
        rnn_model.eval()

        curr_batch_input_ids = defaultdict(list)
        curr_batch_input_masks = defaultdict(list)
        curr_batch_segment_ids = defaultdict(list)

        # Organize the input data for the whole batch in a better way
        for item_paragraph in batch[0]:
            for idx in range(item_paragraph["input_ids"].shape[0]):
                curr_batch_input_ids[idx].append(item_paragraph["input_ids"][idx])
                curr_batch_input_masks[idx].append(item_paragraph["input_mask"][idx])
                curr_batch_segment_ids[idx].append(item_paragraph["segment_ids"][idx])

        curr_batch_outputs_from_rnn = []
        # Process curr batch of filings from the batch
        for my_batch in range(len(curr_batch_input_ids)):
            curr_filing_input_ids = curr_batch_input_ids[my_batch]
            curr_filing_input_masks = curr_batch_input_masks[my_batch]
            curr_filing_segment_ids = curr_batch_segment_ids[my_batch]

            curr_filing_encoded_paragraphs = []
            for input_ids, input_masks, segment_ids in zip(
                curr_filing_input_ids,
                curr_filing_input_masks,
                curr_filing_segment_ids,
            ):
                # Check if all paragraphs are processed
                if (
                    torch.count_nonzero(input_ids) == 0
                    and torch.count_nonzero(input_masks) == 0
                    and torch.count_nonzero(segment_ids) == 0
                ):
                    break

                # batch = tuple(t.to(args.device) for t in batch)
                input_curr_paragraph = {
                    "input_ids": input_ids[None, :].to(args.device),
                    "attention_mask": input_masks[None, :].to(args.device),
                    "token_type_ids": segment_ids[None, :].to(args.device),
                }

                with torch.no_grad():
                    pretrained_model_outputs = pretrained_finbert_model(
                        **input_curr_paragraph
                    )
                    encoded_paragraph = adapter_ensemble_model(
                        pretrained_model_outputs, **input_curr_paragraph
                    )
                curr_filing_encoded_paragraphs.append(encoded_paragraph.squeeze(0))

            """
                Use the RNN and generate the loss for this filing
            """

            # Convert list to tensor
            curr_filing_encoded_paragraphs = torch.stack(curr_filing_encoded_paragraphs)
            curr_filing_encoded_paragraphs = curr_filing_encoded_paragraphs.unsqueeze(
                0
            ).to(args.device)

            with torch.no_grad():
                rnn_output_for_filing = rnn_model(curr_filing_encoded_paragraphs)
            curr_batch_outputs_from_rnn.append(rnn_output_for_filing)

        # Convert list to tensor for RNN outputs
        curr_batch_outputs_from_rnn = torch.stack(curr_batch_outputs_from_rnn)
        curr_batch_outputs_from_rnn = curr_batch_outputs_from_rnn.squeeze(1)
        curr_batch_labels = batch[2].to(args.device).unsqueeze(1)
        tmp_eval_loss = loss_fct(curr_batch_outputs_from_rnn, curr_batch_labels)
        eval_loss += tmp_eval_loss.item()

        nb_eval_steps += 1

    # for batch in tqdm(eval_dataloader, desc="Evaluating"):
    #     pretrained_finbert_model.eval()
    #     adapter_ensemble_model.eval()
    #     rnn_model.eval()
    #     index += 1

    #     batch = tuple(t.to(args.device) for t in batch)
    #     with torch.no_grad():
    #         inputs = {
    #             "input_ids": batch[0],
    #             "attention_mask": batch[1],
    #             "token_type_ids": batch[2]
    #             if args.model_type in ["bert", "xlnet"]
    #             else None,  # XLM and RoBERTa don't use segment_ids
    #             "labels": batch[3],
    #             "start_id": batch[4],
    #         }
    #         # outputs = model(**inputs)
    #         pretrained_model_outputs = pretrained_finbert_model(**inputs)
    #         outputs = adapter_ensemble_model(pretrained_model_outputs, **inputs)
    #         tmp_eval_loss, logits = outputs[:2]

    #         eval_loss += tmp_eval_loss.mean().item()
    #     nb_eval_steps += 1

    #     if preds is None:
    #         preds = logits.detach().cpu().numpy()
    #         out_label_ids = inputs["labels"].detach().cpu().numpy()
    #     else:
    #         preds = np.append(preds, logits.detach().cpu().numpy(), axis=0)
    #         out_label_ids = np.append(
    #             out_label_ids, inputs["labels"].detach().cpu().numpy(), axis=0
    #         )

    eval_loss = eval_loss / nb_eval_steps
    # if args.output_mode == "classification":
    #     preds = np.argmax(preds, axis=1)
    # elif args.output_mode == "regression":
    #     preds = np.squeeze(preds)

    results["loss"] = eval_loss
    output_eval_file = os.path.join(
        args.output_dir, args.my_model_name + "eval_results.txt"
    )
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    with open(output_eval_file, "w") as writer:
        logger.info("***** Eval results  *****")
        for key in sorted(results.keys()):
            logger.info("  %s = %s", key, str(results[key]))
            writer.write("%s = %s\n" % (key, str(results[key])))

    return results


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument(
        "--data_dir",
        default=None,
        type=str,
        required=True,
        help="The input data dir. Should contain the .tsv files (or other data files) for the task.",
    )
    parser.add_argument(
        "--finbert_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model",
    )
    parser.add_argument(
        "--kpi_model_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained KPI model",
    )
    parser.add_argument(
        "--task_name",
        default=None,
        type=str,
        required=True,
        help="The name of the task to train selected in the list: "
        + ", ".join(processors.keys()),
    )
    parser.add_argument(
        "--type_text",
        default="mda_paragraphs",
        type=str,
        required=True,
        help="Can be 'mda_paragraphs' or 'mda_sentences'",
    )

    parser.add_argument("--comment", default="", type=str, help="The comment")
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--percentage_change_type",
        default="percentage_change",
        type=str,
        required=True,
        help="The percentage change type for the label. Can be: percentage_change, percentage_change_standard, percentage_change_min_max",
    )
    parser.add_argument(
        "--freeze_bert",
        default=True,
        type=bool,
        help="freeze the parameters of pretrained model.",
    )
    parser.add_argument(
        "--freeze_adapter",
        default=False,
        type=bool,
        help="freeze the parameters of adapter.",
    )
    parser.add_argument(
        "--rnn_input_size",
        default=768,
        type=int,
        help="Input size for RNN.",
    )
    parser.add_argument(
        "--rnn_hidden_size",
        default=768,
        type=int,
        help="Hidden size for RNN.",
    )
    parser.add_argument(
        "--rnn_num_layers",
        default=2,
        type=int,
        help="Number of layers for RNN.",
    )
    parser.add_argument(
        "--rnn_num_classes",
        default=1,
        type=int,
        help="Output for the regression task for RNN.",
    )
    parser.add_argument(
        "--rnn_dropout_prob",
        default=0.2,
        type=float,
        help="Dropout prob before the Dense layer in the RNN.",
    )
    parser.add_argument("--test_mode", default=0, type=int, help="test freeze adapter")
    parser.add_argument(
        "--fusion_mode",
        type=str,
        default="concat",
        help="the fusion mode for bert feautre and adapter feature |add|concat",
    )
    parser.add_argument(
        "--adapter_transformer_layers",
        default=2,
        type=int,
        help="The transformer layers of adapter.",
    )
    parser.add_argument(
        "--adapter_size", default=768, type=int, help="The hidden size of adapter."
    )
    parser.add_argument(
        "--adapter_list",
        default="0,5,10",
        type=str,
        help="The layer where add an adapter",
    )
    parser.add_argument(
        "--adapter_skip_layers",
        default=3,  # could be 6?
        type=int,
        help="The skip_layers of adapter according to bert layers",
    )
    parser.add_argument(
        "--meta_sec_adaptermodel",
        default="",
        type=str,
        help="the pretrained factual adapter model",
    )

    ## Other parameters
    parser.add_argument(
        "--config_name",
        default="",
        type=str,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        default="",
        type=str,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--cache_dir",
        default="",
        type=str,
        help="Where do you want to store the pre-trained models downloaded from s3",
    )
    parser.add_argument(
        "--max_seq_length",
        default=512,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
        "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument(
        "--do_train", action="store_true", help="Whether to run training."
    )
    parser.add_argument(
        "--do_eval", action="store_true", help="Whether to run eval on the dev set."
    )
    parser.add_argument(
        "--evaluate_during_training",
        action="store_true",
        help="Run evaluation during training at each logging step.",
    )
    parser.add_argument(
        "--kpi_input_size",
        default=116,
        type=int,
        help="Input size for KPI model.",
    )
    parser.add_argument(
        "--kpi_hidden_layers",
        default=1,
        type=int,
        help="Number of hidden layers for the regression task for KPI model.",
    )
    parser.add_argument(
        "--kpi_hidden_size",
        default=64,
        type=int,
        help="Number of neurons in hidden layer for the regression task for KPI model.",
    )
    parser.add_argument(
        "--kpi_dropout_prob",
        default=0.2,
        type=float,
        help="Number of neurons in hidden layer for the regression task for KPI model.",
    )
    parser.add_argument(
        "--kpi_num_classes",
        default=1,
        type=int,
        help="Output for the regression task for KPI model.",
    )
    # parser.add_argument(
    #     "--do_lower_case",
    #     action="store_true",
    #     help="Set this flag if you are using an uncased model.",
    # )

    # parser.add_argument(
    #     "--per_gpu_train_batch_size",
    #     default=8,
    #     type=int,
    #     help="Batch size per GPU/CPU for training.",
    # )
    # parser.add_argument(
    #     "--per_gpu_eval_batch_size",
    #     default=8,
    #     type=int,
    #     help="Batch size per GPU/CPU for evaluation.",
    # )

    parser.add_argument(
        "--train_batch_size",
        default=64,
        type=int,
        help="Batch size for training.",
    )
    parser.add_argument(
        "--eval_batch_size",
        default=64,
        type=int,
        help="Batch size for evaluation.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam.",
    )
    parser.add_argument(
        "--weight_decay", default=0.0, type=float, help="Weight decay if we apply some."
    )
    parser.add_argument(
        "--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer."
    )
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm."
    )
    parser.add_argument(
        "--num_train_epochs",
        default=10,
        type=float,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--warmup_steps", default=0, type=int, help="Linear warmup over warmup_steps."
    )
    parser.add_argument(
        "--logging_steps", type=int, default=10, help="Log every X updates steps."
    )
    parser.add_argument(
        "--save_epoch_steps",
        type=int,
        default=1,
        help="Save checkpoint every X epochs steps.",
    )
    parser.add_argument(
        "--eval_all_checkpoints",
        action="store_true",
        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number",
    )
    parser.add_argument(
        "--no_cuda", action="store_true", help="Avoid using CUDA when available"
    )
    parser.add_argument(
        "--overwrite_output_dir",
        action="store_true",
        help="Overwrite the content of the output directory",
    )
    parser.add_argument(
        "--overwrite_cache",
        action="store_true",
        help="Overwrite the cached training and evaluation sets",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for initialization"
    )
    parser.add_argument(
        "--restore",
        type=bool,
        default=True,
        help="Whether restore from the last checkpoint, is nochenckpoints, start from scartch",
    )

    # parser.add_argument(
    #     "--fp16",
    #     action="store_true",
    #     help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    # )
    # parser.add_argument(
    #     "--fp16_opt_level",
    #     type=str,
    #     default="O1",
    #     help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
    #     "See details at https://nvidia.github.io/apex/amp.html",
    # )
    parser.add_argument(
        "--max_save_checkpoints",
        type=int,
        default=3,
        help="The max amounts of checkpoint saving. Bigger than it will delete the former checkpoints",
    )

    parser.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="For distributed training: local_rank",
    )
    parser.add_argument(
        "--meta_bertmodel", default="", type=str, help="the pretrained bert model"
    )
    parser.add_argument(
        "--save_model_iteration", type=int, help="when to save the model.."
    )

    args = parser.parse_args()

    args.adapter_list = args.adapter_list.split(",")
    args.adapter_list = [int(i) for i in args.adapter_list]

    name_prefix = f"{list(filter(None, args.finbert_path.split('/'))).pop()}_{args.percentage_change_type}_kfold-{args.data_dir.split('_')[-1]}_max_seq-{args.max_seq_length}_rnn_num_layers-{args.rnn_num_layers}_rnn_hidden_size-{args.rnn_hidden_size}_batch-{args.train_batch_size}_lr-{args.learning_rate}_warmup-{args.warmup_steps}_epoch-{args.num_train_epochs}_comment-{args.comment}"
    args.my_model_name = args.task_name + "_" + name_prefix
    args.output_dir = os.path.join(args.output_dir, args.my_model_name)

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu"
        )
        args.n_gpu = torch.cuda.device_count()
    # else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
    #     torch.cuda.set_device(args.local_rank)
    #     device = torch.device("cuda", args.local_rank)
    #     torch.distributed.init_process_group(backend="nccl")
    #     args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )
    # logger.warning(
    #     "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
    #     args.local_rank,
    #     device,
    #     args.n_gpu,
    #     bool(args.local_rank != -1),
    #     args.fp16,
    # )

    logger.warning("Process rank: %s, device: %s", args.local_rank, device)

    # Set seed
    set_seed(args)

    # Choose tokenizer for BERT or FinBERT
    if list(filter(None, args.finbert_path.split("/"))).pop() == "bert-base-uncased":
        tokenizer = BertTokenizerLocal.from_pretrained("bert-base-uncased")
    elif list(filter(None, args.finbert_path.split("/"))).pop() == "FinBERT":
        tokenizer = BertTokenizerHugging.from_pretrained(
            "yiyanghkust/finbert-tone", model_max_length=args.max_seq_length
        )
    pretrained_model = PretrainedModel(args)
    if args.meta_sec_adaptermodel:
        sec_adapter = AdapterModel(args, pretrained_model.config)
        sec_adapter = load_pretrained_adapter(sec_adapter, args.meta_sec_adaptermodel)
    else:
        sec_adapter = None

    adapter_ensemble_model = AdapterEnsembleModel(
        args, pretrained_model.config, sec_adapter=sec_adapter
    )

    rnn_model = RNNModel(args)
    # Load KPI model and freeze params
    kpi_model = KPIModel(args)
    kpi_model.load_state_dict(
        torch.load(args.kpi_model_path, map_location=torch.device(args.device))
    )
    for p in kpi_model.parameters():
        p.requires_grad = False
    kpi_model.eval()

    pretrained_model.to(args.device)
    adapter_ensemble_model.to(args.device)
    rnn_model.to(args.device)
    kpi_model.to(args.device)

    full_ensemble_model = (
        pretrained_model,
        adapter_ensemble_model,
        rnn_model,
        kpi_model,
    )

    logger.info("Training/evaluation parameters %s", args)

    val_dataset = load_and_cache_examples(
        args, args.task_name, tokenizer, "val", evaluate=True
    )

    # Training
    if args.do_train:
        train_dataset = load_and_cache_examples(
            args, args.task_name, tokenizer, "train", evaluate=False
        )
        # print("Features created!")
        # return
        global_step, tr_loss = train(
            args, train_dataset, val_dataset, full_ensemble_model, tokenizer
        )
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

    # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        # Saving Adapter Ensemble BERT like model
        logger.info("Saving Adapter Ensemble model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        adapter_ensemble_model_to_save = (
            adapter_ensemble_model.module
            if hasattr(adapter_ensemble_model, "module")
            else adapter_ensemble_model
        )  # Take care of distributed/parallel training
        adapter_ensemble_model_to_save.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)

        # Saving RNN model
        logger.info("Saving RNN model checkpoint to %s", args.output_dir)
        # Save a trained model
        # They can then be reloaded using `from_pretrained()`
        rnn_model_to_save = (
            rnn_model.module if hasattr(rnn_model, "module") else rnn_model
        )  # Take care of distributed/parallel training
        rnn_model_to_save.save_pretrained(args.output_dir)

    # # Evaluation
    # results = {}
    # if args.do_eval and args.local_rank in [-1, 0]:
    #     tokenizer = tokenizer_class.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case)
    #     checkpoints = [args.output_dir]
    #     if args.eval_all_checkpoints:
    #         checkpoints = list(os.path.dirname(c) for c in sorted(glob.glob(args.output_dir + '/**/' + WEIGHTS_NAME, recursive=True)))
    #         logging.getLogger("pytorch_transformers.modeling_utils").setLevel(logging.WARN)  # Reduce logging
    #     logger.info("Evaluate the following checkpoints: %s", checkpoints)
    #     for checkpoint in checkpoints:
    #         global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
    #         model = model_class.from_pretrained(checkpoint)
    #         model.to(args.device)
    #         result = evaluate(args, model, tokenizer, prefix=global_step)
    #         logger.info('micro f1:{}'.format(result))
    #         result = dict((k + '_{}'.format(global_step), v) for k, v in result.items())
    #         results.update(result)
    # save_result = str(results)
    # save_results.append(save_result)

    # result_file = open(os.path.join(args.output_dir, args.my_model_name + '_result.txt'), 'w')
    # for line in save_results:
    #     result_file.write(str(line) + '\n')
    # result_file.close()

    # return results


if __name__ == "__main__":
    main()

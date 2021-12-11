import pytorch_lightning as pl
import datasets
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from datasets import load_dataset
from transformers import AutoTokenizer
from typing import Optional, Dict
from functools import partial
from tqdm import tqdm


import pytorch_lightning as pl
from transformers.modeling_outputs import MultipleChoiceModelOutput
from transformers import AutoConfig,AutoModel
from transformers import AdamW
import torch
import numpy as np
import torch.nn.functional as F
import torch.nn as nn
from torch.nn import CrossEntropyLoss
from torch.nn import MultiheadAttention

import sys

class SemvalDataModule(pl.LightningDataModule):
    def __init__(
            self,
            model_name_or_path: str = 'google/electra-large-discriminator',
            task_name: str = 'DUMA-electra',
            max_seq_length: int = 256,
            train_batch_size: int = 2,
            eval_batch_size: int = 2,
    ):
        super().__init__()
        self.model_name_or_path = model_name_or_path
        self.task_name = task_name
        self.max_seq_length = max_seq_length
        self.train_batch_size = train_batch_size
        self.eval_batch_size = eval_batch_size

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path, use_fast=True)
        self.dataset = None

        # self.encoded_dataset = None

    def setup(self, stage: Optional[str] = None):
        preprocessor = partial(self.preprocess, self.tokenizer)
        if stage == 'fit':
            self.dataset = load_dataset('json', data_files={'train': sys.argv[1],
                                                            'dev': sys.argv[2]})

            print('Encoding the training datset...')
            # print(preprocessor(self.dataset['train'][0]))
            self.dataset['train'] = self.dataset['train'].map(preprocessor)
            print('Encoding the validation datset...')
            self.dataset['dev'] = self.dataset['dev'].map(preprocessor)
            print(self.dataset)
            # print(self.dataset['dev'][0]['input_ids'])
            self.dataset['train'].set_format(type='torch', columns=['input_ids', 'attention_mask', 'label'])
            self.dataset['dev'].set_format(type='torch', columns=['input_ids', 'attention_mask', 'label'])
            print(self.dataset['dev'][0]['input_ids'])

    def train_dataloader(self):
        return DataLoader(self.dataset['train'],
                          sampler=RandomSampler(self.dataset['train']),
                          batch_size=self.train_batch_size,
                          drop_last=True,
                          )

    def val_dataloader(self):
        return DataLoader(self.dataset['dev'],
                          sampler=RandomSampler(self.dataset['dev']),
                          batch_size=self.eval_batch_size,
                          drop_last=True,
                          )

    @staticmethod
    def preprocess(tokenizer, x: Dict) -> Dict:

        choices_features = []
        option_names = ['option_0', 'option_1', 'option_2', 'option_3', 'option_4']

        question = x["question"]
        article = x["article"]

        for option in option_names:
            question_option = question.replace("@placeholder", x[option])

            inputs = tokenizer(
                article,
                question_option,
                add_special_tokens=True,
                max_length=256,
                truncation="only_first",
                padding='max_length',
                return_tensors='pt'
            )

            choices_features.append(inputs)

        label = torch.tensor([x["label"]])

        return {
            "label": label,
            "input_ids": torch.cat([cf["input_ids"] for cf in choices_features]).reshape(-1),
            "attention_mask": torch.cat([cf["attention_mask"] for cf in choices_features]).reshape(-1),
            # "token_type_ids": torch.cat([cf["token_type_ids"] for cf in choices_features]).reshape(-1),
        }


class DUMAForSemval(pl.LightningModule):
    def __init__(
            self,
            pretrained_model: str = 'google/electra-large-discriminator',
            learning_rate: float = 1e-4,
            gradient_accumulation_steps: int = 32,
            num_train_epochs: float = 4.0,
            train_batch_size: int = 2,
            train_all: bool = False,
    ):
        super().__init__()
        self.config = AutoConfig.from_pretrained(pretrained_model)
        self.electra = AutoModel.from_pretrained(pretrained_model, config=self.config)
        self.classifier = nn.Linear(self.config.hidden_size, 1)

        if not train_all:
            for param in self.electra.parameters():
                param.requires_grad = False

        self.learning_rate = learning_rate
        self.gradient_accumulation_steps = gradient_accumulation_steps
        self.num_train_epochs = num_train_epochs
        self.train_batch_size = train_batch_size

    def forward(
            self,
            input_ids=None,  # (batch_size,num_choices,sequence_length:256)
            attention_mask=None,
            # token_type_ids=None,
            labels=None,
    ):

        input_ids = input_ids.reshape(self.train_batch_size, 5, -1)
        attention_mask = attention_mask.reshape(self.train_batch_size, 5, -1)
        # token_type_ids = token_type_ids.reshape(self.train_batch_size,5,-1)

        # print(input_ids)
        # print(input_ids.shape)

        num_choices = input_ids.shape[1]

        input_ids = input_ids.view(-1, input_ids.size(-1))  # (batch_size*num_choice,sequence_length:256)
        # print(input_ids)
        attention_mask = attention_mask.view(-1, attention_mask.size(-1))
        # token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1))

        outputs = self.electra(
            input_ids=input_ids,
            attention_mask=attention_mask,
            # token_type_ids=token_type_ids,
        )

        last_output = outputs.last_hidden_state  # (batch_size, sequence_length:256, hidden_size:256)

        pooled_output = torch.mean(last_output, dim=1)
        logits = self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, num_choices)

        loss = None
        loss_fct = CrossEntropyLoss()
        loss = loss_fct(reshaped_logits, labels)

        # output = (reshaped_logits,) + outputs[2:]
        # return ((loss,) + output) if loss is not None else output

        return MultipleChoiceModelOutput(
            loss=loss,
            logits=reshaped_logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def training_step(self, batch, batch_idx):
        # input training batch, calling DUMA forward() function
        # return loss
        outputs = self(
            input_ids=batch['input_ids'],
            attention_mask=batch['attention_mask'],
            # token_type_ids=batch['token_type_ids'],
            labels=batch['label'],
        )
        labels_hat = torch.argmax(outputs.logits, dim=1)
        correct_count = torch.sum(batch['label'] == labels_hat)
        loss = outputs.loss
        self.log('train_loss', loss)
        self.log('train_acc', correct_count.float() / len(batch['label']))
        # print('train_acc',correct_count.float() / len(batch['label']))

        return loss

    def validation_step(self, batch, batch_idx):
        # input validation batch, calling DUMA forward() function
        # return loss
        outputs = self(
            input_ids=batch['input_ids'],
            attention_mask=batch['attention_mask'],
            # token_type_ids=batch['token_type_ids'],
            labels=batch['label'],
        )
        labels_hat = torch.argmax(outputs.logits, dim=1)
        correct_count = torch.sum(batch['label'] == labels_hat)
        loss = outputs.loss

        return {
            "val_loss": loss,
            "correct_count": correct_count,
            "batch_size": len(batch['label'])
        }

    def validation_epoch_end(self, outputs) -> None:
        val_acc = sum([out["correct_count"] for out in outputs]).float() / sum(out["batch_size"] for out in outputs)
        val_loss = sum([out["val_loss"] for out in outputs]) / len(outputs)
        self.log('val_acc', val_acc)
        self.log('val_loss', val_loss)
        print('val_loss', val_loss)
        print('val_acc', val_acc)

    def configure_optimizers(self):
        param_optimizer = list(self.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        return AdamW(optimizer_grouped_parameters, lr=self.learning_rate)

if __name__ == "__main__":
    model_name = 'roberta-large'
    model = DUMAForSemval(
        pretrained_model=model_name,
        learning_rate=1e-4,
        num_train_epochs=1.0,
        train_batch_size=2,
        train_all=False,
    )
    data = SemvalDataModule(
        model_name_or_path=model_name,
        train_batch_size=2,
        eval_batch_size=2,
        max_seq_length=256,
    )
    trainer = pl.Trainer(
        gpus=1,
        # auto_scale_batch_size='power',
        # auto_lr_find=True,
        max_epochs=1,
        val_check_interval=0.2,
    )
    trainer.fit(model, data)
    trainer.save_checkpoint('roberta-large_sem/')
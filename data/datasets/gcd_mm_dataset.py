import numpy as np
import os
from pathlib import Path
from collections import defaultdict
import torch
from torch.utils.data import Dataset
from typing import List, Tuple, Optional, Dict
from transformers import CLIPImageProcessor, PreTrainedTokenizer
import cv2
import random
import json
# My
from data.garment_tokenizers.gcd_garment_tokenizer import GCDGarmentTokenizer
from data.patterns.pattern_converter import NNSewingPattern
from data.patterns.panel_classes import PanelClasses
from data.datasets.utils import (SHORT_QUESTION_LIST, 
                                 ANSWER_LIST, 
                                 DEFAULT_PLACEHOLDER_TOKEN, 
                                 DESCRIPTIVE_TEXT_SHORT_QUESTION_LIST, 
                                 SPECULATIVE_TEXT_SHORT_QUESTION_LIST, 
                                 SHORT_QUESTION_WITH_TEXT_LIST,
                                 EDITING_QUESTION_LIST,
                                 SampleToTensor
                                 )
from models.llava import conversation as conversation_lib
from data.transforms import ResizeLongestSide
from enum import Enum

class DataType(Enum):
    IMAGE = 0
    DESCRIPTIVE_TEXT = 1
    SPECULATIVE_TEXT = 2
    IMAGE_TEXT = 3  
    EDITING = 4
    def requires_text(self):
        return self in [DataType.IMAGE_TEXT, DataType.DESCRIPTIVE_TEXT, DataType.SPECULATIVE_TEXT]
    @classmethod
    def list(cls):
        return list(map(lambda c: c, cls))

## incorperating the changes from maria's three dataset classes into a new
## dataset class. this also includes features from sewformer, for interoperability
class GCDMM(Dataset):  
    def __init__(
        self, 
        root_dir: str, 
        editing_dir: str, 
        caption_dir: str, 
        sampling_rate: List[int],
        vision_tower: str,
        image_size: int, 
        editing_flip_prob: float,
        load_by_dataname: List[Tuple[str, str, str]],
        garment_tokenizer: GCDGarmentTokenizer,
        panel_classification: Optional[str]=NotImplemented): 

        self.editing_dir = editing_dir
        self.editing_flip_prob = editing_flip_prob
        self.caption_dir = caption_dir
        self.sampling_rate=sampling_rate
        self.panel_classifier = None
        self.panel_classification = panel_classification
        
        #################################
        # init from the basedataset class
        self.root_path = Path(root_dir)

        self.datapoints_names = []
        self.panel_classes = []
        self.dict_panel_classes = defaultdict(int)
        self.garment_tokenizer = garment_tokenizer
        
        self.clip_image_processor = CLIPImageProcessor.from_pretrained(vision_tower)
        self.vision_tower=vision_tower
        self.image_size = image_size    
        self.transform = ResizeLongestSide(image_size)
        self.short_question_list = SHORT_QUESTION_LIST
        self.descriptive_text_question_list = DESCRIPTIVE_TEXT_SHORT_QUESTION_LIST
        self.speculative_text_question_list = SPECULATIVE_TEXT_SHORT_QUESTION_LIST
        self.text_image_question_list = SHORT_QUESTION_WITH_TEXT_LIST
        self.editing_question_list = EDITING_QUESTION_LIST
        self.answer_list = ANSWER_LIST

        self.information = defaultdict(dict)  # TODO REMOVE Needed? Should be part of experiment object/initial config? I don't see it used at all 
        ## collect information about data folders: 

        if isinstance(load_by_dataname, str) and load_by_dataname.endswith('.txt'):
            with open(load_by_dataname, 'r') as f:
                load_by_dataname = [line.strip() for line in f.readlines()]
        self.datapoints_names = load_by_dataname
        # Wrong Don't use
        
           
                
        self.panel_classifier = PanelClasses(classes_file=panel_classification)
        self.panel_classes = self.panel_classifier.classes

        print("The panel classes in this dataset are :", self.panel_classes)
        

        self.gt_cached = {}
        self.gt_caching = True

        # Use default tensor transform + the ones from input
        self.transforms = [SampleToTensor()]

        ########################################
        # end of init from the basedataset class
        
        # self._drop_cache()
        self.gt_cached = {}


    # added from maria 
    def __len__(self):
        """Number of entries in the dataset"""
        return len(self.datapoints_names)  

    def _fetch_pattern(self, idx):
        """Fetch the pattern and image for the given index"""
        _, garment_names, garment_spec = self.datapoints_names[idx]
        gt_pattern = NNSewingPattern(garment_spec)
        gt_pattern.name = "_".join(garment_names)
        return gt_pattern
    
    def _parepare_image(self, image_paths):
        """Fetch the image for the given index"""
        image_path = random.choice(image_paths)
        image = cv2.imread(image_path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image_clip = self.clip_image_processor.preprocess(image, return_tensors="pt")["pixel_values"][0]  # preprocess image for clip
        return image_clip, image_path
    
    def get_mode_names(self):
        return ['image', 'description','occasion', 'text_image', 'editing']

    # added from maria 
    def __getitem__(self, idx):
        """Called when indexing: read the corresponding data. 
        Does not support list indexing"""
        
        if torch.is_tensor(idx):  # allow indexing by tensors
            idx = idx.tolist()

        datapoint_name = self.datapoints_names[idx]
        data_name = datapoint_name.split('/')[-1]
        image_paths = [
            os.path.join(self.root_path, datapoint_name, f'{data_name}_render_back.png'),
            os.path.join(self.root_path, datapoint_name, f'{data_name}_render_front.png')
        ]
        if data_name in self.gt_cached:
            gt_pattern, pattern_dict, edited_pattern, edited_pattern_dict, editing_captions, captions = self.gt_cached[data_name]
        else:
            spec_file = os.path.join(self.root_path, datapoint_name, f'{data_name}_specification_shifted.json')
            gt_pattern = NNSewingPattern(spec_file, panel_classifier=self.panel_classifier, template_name=data_name)
            gt_pattern.name = data_name
            pattern_dict = self.garment_tokenizer.encode(gt_pattern)
            
            editing_spec_file = os.path.join(self.editing_dir, data_name, f'edited_specification.json')
            editing_caption_json = os.path.join(self.editing_dir, data_name, f'editing_caption.json')
            if (not os.path.exists(editing_spec_file)) or (not os.path.exists(editing_caption_json)):
                edited_pattern = None
                edited_pattern_dict = None
                editing_captions = None
            else:
                edited_pattern = NNSewingPattern(editing_spec_file, panel_classifier=self.panel_classifier, template_name=data_name)
                edited_pattern.name = data_name
                edited_pattern_dict = self.garment_tokenizer.encode(edited_pattern)
                editing_captions = json.load(open(editing_caption_json, 'r'))
            
            caption_json = os.path.join(self.caption_dir, data_name, f'captions.json')
            if os.path.exists(caption_json):
                captions = json.load(open(caption_json, 'r'))
            else:
                captions = None
            
            self.gt_cached[data_name] = (gt_pattern, pattern_dict, edited_pattern, edited_pattern_dict, editing_captions, captions)
            
            
        image_clip = torch.zeros((3, 224, 224))
        image_path = ''
        sample_type = np.random.choice(list(DataType), p=self.sampling_rate)
        if sample_type == DataType.EDITING and edited_pattern is None:
            sample_type = DataType.IMAGE  # no editing if there is no edited pattern
        if DataType.requires_text(sample_type) and captions is None:
            sample_type = DataType.IMAGE  # no text if there is no caption
        if sample_type == DataType.IMAGE:
            # image_only
            image_clip, image_path = self._parepare_image(image_paths)
            # questions and answers
            questions = []
            answers = []
            for i in range(1):
                question_template = random.choice(self.short_question_list)
                questions.append(question_template)
                answer_template = random.choice(self.answer_list).format(pattern=DEFAULT_PLACEHOLDER_TOKEN)
                answers.append(answer_template)
            out_pattern_dict = pattern_dict
            question_pattern_dict = {}
            out_pattern = [gt_pattern]
        elif sample_type == DataType.DESCRIPTIVE_TEXT:
            # descriptive text_only
            descriptive_text = captions['description']
            # questions and answers
            questions = []
            answers = []
            for i in range(1):
                question_template = random.choice(self.descriptive_text_question_list).format(sent=descriptive_text)
                questions.append(question_template)
                answer_template = random.choice(self.answer_list).format(pattern=DEFAULT_PLACEHOLDER_TOKEN)
                answers.append(answer_template)
            out_pattern_dict = pattern_dict
            question_pattern_dict = {}
            out_pattern = [gt_pattern]
        elif sample_type == DataType.SPECULATIVE_TEXT:
            # speculative text_only
            speculative_text = captions['occasion']
            # questions and answers
            questions = []
            answers = []
            for i in range(1):
                question_template = random.choice(self.speculative_text_question_list).format(sent=speculative_text)
                questions.append(question_template)
                answer_template = random.choice(self.answer_list).format(pattern=DEFAULT_PLACEHOLDER_TOKEN)
                answers.append(answer_template)
            out_pattern_dict = pattern_dict
            question_pattern_dict = {}
            out_pattern = [gt_pattern]
        elif sample_type == DataType.IMAGE_TEXT:
            # image_text
            descriptive_text = captions['description']
            image_clip, image_path = self._parepare_image(image_paths)
            # questions and answers
            questions = []
            answers = []
            for i in range(1):
                question_template = random.choice(self.text_image_question_list).format(sent=descriptive_text)
                questions.append(question_template)
                answer_template = random.choice(self.answer_list).format(pattern=DEFAULT_PLACEHOLDER_TOKEN)
                answers.append(answer_template)
            out_pattern_dict = pattern_dict
            question_pattern_dict = {}
            out_pattern = [gt_pattern]
        elif sample_type == DataType.EDITING:
            # garment_editing
            if random.random() > self.editing_flip_prob:
                before_pattern_dict = pattern_dict
                after_pattern_dict = edited_pattern_dict
                editing_text = editing_captions['editing_description_forward']
                gt_pattern.name = "before_" + gt_pattern.name
                edited_pattern.name = "after_" + edited_pattern.name
                out_pattern = [gt_pattern, edited_pattern]
            else:
                before_pattern_dict = edited_pattern_dict
                after_pattern_dict = pattern_dict
                editing_text = editing_captions['editing_description_reverse']
                edited_pattern.name = "before_" + edited_pattern.name
                gt_pattern.name = "after_" + gt_pattern.name
                out_pattern = [edited_pattern, gt_pattern]
            # questions and answers
            questions = []
            answers = []
            for i in range(1):
                question_template = random.choice(self.editing_question_list).format(pattern=DEFAULT_PLACEHOLDER_TOKEN, sent=editing_text)
                questions.append(question_template)
                answer_template = random.choice(self.answer_list).format(pattern=DEFAULT_PLACEHOLDER_TOKEN)
                answers.append(answer_template)
            out_pattern_dict = {k: before_pattern_dict[k] + after_pattern_dict[k] for k in before_pattern_dict.keys()}
            question_pattern_dict = before_pattern_dict
        else:
            raise ValueError(f"Invalid sample type: {sample_type}")

        conversations = []
        question_only_convs = []
        conv = conversation_lib.default_conversation.copy()

        i = 0
        while i < len(questions):
            conv.messages = []
            conv.append_message(conv.roles[0], questions[i])
            conv.append_message(conv.roles[1], answers[i])
            conversations.append(conv.get_prompt())
            conv.messages = []
            conv.append_message(conv.roles[0], questions[i])
            conv.append_message(conv.roles[1], "")
            question_only_convs.append(conv.get_prompt())
            i += 1

        return (
            out_pattern_dict,
            question_pattern_dict,
            image_path,
            image_clip,
            conversations,
            question_only_convs,
            questions,
            out_pattern,
            sample_type.value
        ) 
    
    @property
    def panel_edge_type_indices(self):
        return self.garment_tokenizer.panel_edge_type_indices
    
    @property
    def gt_stats(self):
        return self.garment_tokenizer.gt_stats
    
    def get_all_token_names(self):
        return self.garment_tokenizer.get_all_token_names()
    
    def set_token_indices(self, token2idx: Dict[str, int]):
        return self.garment_tokenizer.set_token_indices(token2idx)
    
    def evaluate_patterns(self, pred_patterns: List[NNSewingPattern], gt_patterns: List[NNSewingPattern]):
        return self.garment_tokenizer.evaluate_patterns(pred_patterns, gt_patterns)
    
    def get_item_infos(self, index):
        return self.datapoints_names[index]
    
    def decode(self, output_ids: torch.Tensor, tokenizer: PreTrainedTokenizer):
        """Decode output ids to text"""
        return self.garment_tokenizer.decode(output_ids, tokenizer)
    
    def split_from_dict(self, split_dict):
        """
            Reproduce the data split in the provided dictionary: 
            the elements of the currect dataset should play the same role as in provided dict
        """
        train_ids, valid_ids, test_ids = [], [], []
        
        training_datanames = split_dict['train']
        valid_datanames = split_dict['validation']
        test_datanames = split_dict['test']

        for idx in range(len(self.datapoints_names)):
            if self.datapoints_names[idx] in training_datanames:  # usually the largest, so check first
                train_ids.append(idx)
            elif self.datapoints_names[idx] in test_datanames:
                test_ids.append(idx)
            elif self.datapoints_names[idx] in valid_datanames:
                valid_ids.append(idx)
            else:
                continue
            
            if idx % 1000 == 0:
                print(f"progress {idx}, #Train_Ids={len(train_ids)}, #Valid_Ids={len(valid_ids)}, #Test_Ids={len(test_ids)}")
        
        return (
            [self.datapoints_names[i] for i in train_ids], 
            [self.datapoints_names[i] for i in valid_ids],
            [self.datapoints_names[i] for i in test_ids] if len(test_ids) > 0 else None
        )
    
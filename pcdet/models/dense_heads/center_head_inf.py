import copy
import torch
import torch.nn as nn
from torch.nn.init import kaiming_normal_
from ..model_utils import model_nms_utils
from ..model_utils import centernet_utils
from ...utils import loss_utils
from typing import Dict, List, Tuple, Optional, Final
from functools import partial

class SeparateHead(nn.Module):
    vel_conv_available : Final[bool]
    iou_conv_available : Final[bool]

    def __init__(self, input_channels, sep_head_dict, init_bias=-2.19, use_bias=False, norm_func=None, enable_normalization=True):
        super().__init__()
        self.sep_head_dict = sep_head_dict
        self.conv_names = tuple(sep_head_dict.keys())

        for cur_name in self.sep_head_dict:
            output_channels = self.sep_head_dict[cur_name]['out_channels']
            num_conv = self.sep_head_dict[cur_name]['num_conv']

            fc_list = []
            for k in range(num_conv - 1):
                inner_fc_list = [nn.Conv2d(input_channels, input_channels, kernel_size=3, stride=1, padding=1, bias=use_bias)]
                if enable_normalization: #TODO I havent made an exception for hm, but its ok
                    inner_fc_list.append(nn.BatchNorm2d(input_channels) if norm_func is None else norm_func(input_channels))
                inner_fc_list.append(nn.ReLU())
                fc_list.append(nn.Sequential(*inner_fc_list))
            fc_list.append(nn.Conv2d(input_channels, output_channels, kernel_size=3, stride=1, padding=1, bias=True))
            fc = nn.Sequential(*fc_list)

            if 'hm' in cur_name:
                fc[-1].bias.data.fill_(init_bias)
            else:
                for m in fc.modules():
                    if isinstance(m, nn.Conv2d):
                        kaiming_normal_(m.weight.data)
                        if hasattr(m, "bias") and m.bias is not None:
                            nn.init.constant_(m.bias, 0)

            self.__setattr__(cur_name, fc)

        self.vel_conv_available = ('vel' in self.sep_head_dict)
        self.iou_conv_available = ('iou' in self.sep_head_dict)

    def forward_hm(self, x) -> Dict[str, torch.Tensor]:
        return {'hm': self.hm(x).sigmoid()}

    def forward_attr(self, x) -> Dict[str, torch.Tensor]:
        ret_dict = {
            'center': self.center(x),
            'center_z': self.center_z(x),
            'dim': self.dim(x),
            'rot': self.rot(x),
            }
        if self.vel_conv_available:
            ret_dict['vel'] = self.vel(x)
        if self.iou_conv_available:
            ret_dict['iou'] = self.iou(x)

        return ret_dict

    def forward(self, x : torch.Tensor) -> Dict[str, torch.Tensor]:
        ret_dict = self.forward_hm(x)
        ret_dict.update(self.forward_attr(x))
        return ret_dict

# Inference only, torchscript compatible
class CenterHeadInf(nn.Module): 
    point_cloud_range : Final[List[float]]
    voxel_size : Final[List[float]]
    feature_map_stride : Final[int]
    nms_type : Final[str]
    nms_thresh : Final[List[float]]
    nms_pre_maxsize : Final[List[int]]
    nms_post_maxsize : Final[List[int]]
    score_thresh : Final[float]
    use_iou_to_rectify_score : Final[bool]
    head_order : Final[List[str]]
    max_obj_per_sample : Final[int]

    class_id_mapping_each_head : List[torch.Tensor]
    det_dict_copy : Dict[str,torch.Tensor]
    iou_rectifier : torch.Tensor
    post_center_limit_range : torch.Tensor

    def __init__(self, model_cfg, input_channels, num_class, class_names, grid_size, point_cloud_range, voxel_size,
                 predict_boxes_when_training=False):
        super().__init__()
        assert not predict_boxes_when_training
        self.model_cfg = model_cfg
        self.num_class = num_class
        self.grid_size = grid_size
        self.point_cloud_range = point_cloud_range.tolist()
        self.voxel_size = voxel_size
        self.feature_map_stride = self.model_cfg.TARGET_ASSIGNER_CONFIG.get('FEATURE_MAP_STRIDE', 1)

        self.class_names = class_names
        self.class_names_each_head = []
        class_id_mapping_each_head : List[torch.Tensor] = []

        for cur_class_names in self.model_cfg.CLASS_NAMES_EACH_HEAD:
            self.class_names_each_head.append([x for x in cur_class_names if x in class_names])
            cur_class_id_mapping = torch.tensor(
                [self.class_names.index(x) for x in cur_class_names if x in class_names]
            ).cuda()
            class_id_mapping_each_head.append(cur_class_id_mapping)
        self.class_id_mapping_each_head = class_id_mapping_each_head

        total_classes = sum([len(x) for x in self.class_names_each_head])
        assert total_classes == len(self.class_names), f'class_names_each_head={self.class_names_each_head}'

        self.cls_id_to_det_head_idx_map = torch.zeros((total_classes,), dtype=torch.int)
        self.num_det_heads = len(self.class_id_mapping_each_head)
        for i, cls_ids in enumerate(self.class_id_mapping_each_head):
            for cls_id in cls_ids:
                self.cls_id_to_det_head_idx_map[cls_id] = i

        norm_func = partial(nn.BatchNorm2d, eps=self.model_cfg.get('BN_EPS', 1e-5), momentum=self.model_cfg.get('BN_MOM', 0.1))
        self.shared_conv = nn.Sequential(
            nn.Conv2d(
                input_channels, self.model_cfg.SHARED_CONV_CHANNEL, 3, stride=1, padding=1,
                bias=self.model_cfg.get('USE_BIAS_BEFORE_NORM', False)
            ),
            norm_func(self.model_cfg.SHARED_CONV_CHANNEL),
            nn.ReLU(),
        )

        self.heads_list = nn.ModuleList()
        self.separate_head_cfg = self.model_cfg.SEPARATE_HEAD_CFG
        for idx, cur_class_names in enumerate(self.class_names_each_head):
            cur_head_dict = copy.deepcopy(self.separate_head_cfg.HEAD_DICT)
            cur_head_dict['hm'] = dict(out_channels=len(cur_class_names), num_conv=self.model_cfg.NUM_HM_CONV)
            self.heads_list.append(
                SeparateHead(
                    input_channels=self.model_cfg.SHARED_CONV_CHANNEL,
                    sep_head_dict=cur_head_dict,
                    init_bias=-2.19,
                    use_bias=self.model_cfg.get('USE_BIAS_BEFORE_NORM', False),
                    norm_func=norm_func,
                    enable_normalization=self.model_cfg.get('ENABLE_NORM_IN_ATTR_LAYERS', True)
                )
            )
        self.det_dict_copy = {
            "pred_boxes": torch.zeros([0, 9], dtype=torch.float, device='cuda'),
            "pred_scores": torch.zeros([0], dtype=torch.float,device='cuda'),
            "pred_labels": torch.zeros([0], dtype=torch.int, device='cuda'),
        }

        post_process_cfg = self.model_cfg.POST_PROCESSING
        self.post_center_limit_range = torch.tensor(post_process_cfg.POST_CENTER_LIMIT_RANGE).cuda().float()
        self.nms_type = post_process_cfg.NMS_CONFIG.NMS_TYPE

        nms_thresh = post_process_cfg.NMS_CONFIG.NMS_THRESH
        nms_pre_maxsize = post_process_cfg.NMS_CONFIG.NMS_PRE_MAXSIZE
        nms_post_maxsize = post_process_cfg.NMS_CONFIG.NMS_POST_MAXSIZE
        self.nms_thresh = nms_thresh if isinstance(nms_thresh, list) else [nms_thresh]
        self.nms_pre_maxsize = nms_pre_maxsize if isinstance(nms_pre_maxsize, list) else [nms_pre_maxsize]
        self.nms_post_maxsize = nms_post_maxsize if isinstance(nms_post_maxsize, list) else [nms_post_maxsize]

        self.score_thresh = post_process_cfg.SCORE_THRESH
        self.use_iou_to_rectify_score = post_process_cfg.get('USE_IOU_TO_RECTIFY_SCORE', False)
        self.iou_rectifier = torch.tensor(post_process_cfg.IOU_RECTIFIER if self.use_iou_to_rectify_score else [0], dtype=torch.float)
        self.head_order = self.separate_head_cfg.HEAD_ORDER
        self.max_obj_per_sample = post_process_cfg.MAX_OBJ_PER_SAMPLE

    def generate_predicted_boxes(self, batch_size: int, pred_dicts: List[Dict[str,torch.Tensor]],\
            topk_outputs : List[List[torch.Tensor]], forecasted_dets : Optional[List[Dict[str,torch.Tensor]]]) \
            -> List[Dict[str,torch.Tensor]]:

        ret_dict : List[Dict[str,List[torch.Tensor]]] = [{
            'pred_boxes': [],
            'pred_scores': [],
            'pred_labels': [],
        } for k in range(batch_size)]
        for idx, pred_dict in enumerate(pred_dicts):
            batch_hm = pred_dict['hm'] #.sigmoid()
            batch_center = pred_dict['center']
            batch_center_z = pred_dict['center_z']
            batch_dim = pred_dict['dim'].exp()
            batch_rot_cos = pred_dict['rot'][:, 0].unsqueeze(dim=1)
            batch_rot_sin = pred_dict['rot'][:, 1].unsqueeze(dim=1)
            batch_vel = pred_dict['vel'] if 'vel' in self.head_order else None
            batch_iou = (pred_dict['iou'] + 1) * 0.5 if 'iou' in pred_dict else None

            final_pred_dicts = centernet_utils.decode_bbox_from_heatmap(
                batch_hm, batch_rot_cos, batch_rot_sin,
                batch_center, batch_center_z, batch_dim,
                self.point_cloud_range, self.voxel_size,
                self.feature_map_stride, self.max_obj_per_sample,
                self.post_center_limit_range, topk_outputs[idx],
                batch_vel, batch_iou, self.score_thresh
            )

            for k, final_dict in enumerate(final_pred_dicts):
                final_dict['pred_labels'] = self.class_id_mapping_each_head[idx][final_dict['pred_labels'].long()]

                if self.use_iou_to_rectify_score and 'pred_iou' in final_dict:
                    pred_iou = torch.clamp(final_dict['pred_iou'], min=0, max=1.0)
                    iou_rec = final_dict['pred_scores'].new_tensor(self.iou_rectifier)
                    final_dict['pred_scores'] = torch.pow(final_dict['pred_scores'], 1 - iou_rec[final_dict['pred_labels']]) * \
                            torch.pow(pred_iou, iou_rec[final_dict['pred_labels']])

                if self.nms_type not in ['circle_nms', 'multi_class_nms']:
                    if forecasted_dets is not None:
                        # get the forecasted_dets that match and cat them for NMS
                        for j in forecasted_dets[idx].keys():
                            final_dict[j] = torch.cat((final_dict[j], forecasted_dets[idx][j].cuda()), dim=0)

                    selected, selected_scores = model_nms_utils.class_agnostic_nms(
                        final_dict['pred_scores'], final_dict['pred_boxes'], self.nms_type, self.nms_thresh[0],
                        self.nms_post_maxsize[0], self.nms_pre_maxsize[0])
                        #score_thresh=None
                elif self.nms_type == 'multi_class_nms':
                    if forecasted_dets is not None:
                        # get the forecasted_dets that match and cat them for NMS
                        for j in forecasted_dets[idx].keys():
                            final_dict[j] = torch.cat((final_dict[j], forecasted_dets[idx][j].cuda()), dim=0)

                    selected, selected_scores = model_nms_utils.multi_classes_nms_mmdet(
                        final_dict['pred_scores'], final_dict['pred_boxes'], final_dict['pred_labels'],
                        self.nms_thresh, self.nms_post_maxsize, self.nms_pre_maxsize, None
                    )
                else:
                    selected = torch.ones(final_dict['pred_boxes'].size(0), dtype=torch.long, device=final_dict['pred_boxes'].device)
                    selected_scores = final_dict['pred_scores']

                final_dict['pred_boxes'] = final_dict['pred_boxes'][selected]
                final_dict['pred_scores'] = selected_scores
                final_dict['pred_labels'] = final_dict['pred_labels'][selected]

                ret_dict[k]['pred_boxes'].append(final_dict['pred_boxes'])
                ret_dict[k]['pred_scores'].append(final_dict['pred_scores'])
                ret_dict[k]['pred_labels'].append(final_dict['pred_labels'])

        final_ret_dict : List[Dict[str,torch.Tensor]] = []
        for k in range(batch_size):
            if not ret_dict[k]['pred_boxes']:
                final_ret_dict.append(self.get_empty_det_dict())
            else:
                final_ret_dict.append({
                    'pred_boxes': torch.cat(ret_dict[k]['pred_boxes'], dim=0),
                    'pred_scores' : torch.cat(ret_dict[k]['pred_scores'], dim=0),
                    'pred_labels' : torch.cat(ret_dict[k]['pred_labels'], dim=0) + 1})

        return final_ret_dict

    def ordered_outp_names(self):
        names =  ['hm'] + list(self.separate_head_cfg.HEAD_ORDER)
        if 'iou' in self.separate_head_cfg.HEAD_DICT:
            names += ['iou']
        return names

    def forward_up_to_topk(self, spatial_features_2d : torch.Tensor) -> List[torch.Tensor]:
        x = self.shared_conv(spatial_features_2d)
        pred_dicts = [h.forward(x) for h in self.heads_list]
        conv_order = self.ordered_outp_names()
        out_tensors_ordered = [pd[conv_name] for pd in pred_dicts for conv_name in conv_order]
        return out_tensors_ordered

    def convert_out_to_batch_dict(self, out_tensors):
        head_order = self.ordered_outp_names()
        num_convs_per_head = len(out_tensors) // self.num_det_heads
        pred_dicts = []
        for i in range(self.num_det_heads):
            ot = out_tensors[i*num_convs_per_head:(i+1)*num_convs_per_head]
            pred_dicts.append({name : t for name, t in zip(head_order, ot)})
        return pred_dicts

    def forward(self, spatial_features_2d : torch.Tensor, forecasted_dets : Optional[List[Dict[str,torch.Tensor]]]):
        assert not self.training
        x = self.shared_conv(spatial_features_2d)
        pred_dicts = self.forward_pre(x)
        pred_dicts = self.forward_post(x, pred_dicts)
        topk_outputs = self.forward_topk(pred_dicts)
        return self.forward_genbox(x.size(0), pred_dicts, topk_outputs, forecasted_dets)

    def forward_pre(self, x) -> List[Dict[str,torch.Tensor]]:
        return [head.forward_hm(x) for head in self.heads_list]

    def forward_post(self, x : torch.Tensor, pred_dicts : List[Dict[str,torch.Tensor]]) -> List[Dict[str,torch.Tensor]]:
        for i, head in enumerate(self.heads_list):
            pred_dicts[i].update(head.forward_attr(x))
        return pred_dicts

    @torch.jit.export
    def forward_topk(self, pred_dicts : List[Dict[str,torch.Tensor]]) -> List[List[torch.Tensor]]:
        return [centernet_utils._topk(pd['hm'], K=self.max_obj_per_sample) for pd in pred_dicts]

    @torch.jit.export
    def forward_genbox(self, batch_size: int, pred_dicts: List[Dict[str,torch.Tensor]],\
            topk_outputs : List[List[torch.Tensor]], forecasted_dets : Optional[List[Dict[str,torch.Tensor]]]) \
            -> List[Dict[str,torch.Tensor]]:
        return self.generate_predicted_boxes(batch_size, pred_dicts, topk_outputs, forecasted_dets)

    def get_empty_det_dict(self):
        det_dict = {}
        for k,v in self.det_dict_copy.items():
            det_dict[k] = v.clone().detach()
        return det_dict

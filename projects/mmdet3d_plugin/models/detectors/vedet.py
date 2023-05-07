# ------------------------------------------------------------------------
# Copyright (c) 2022 Toyota Research Institute, Dian Chen. All Rights Reserved.
# ------------------------------------------------------------------------
# Copyright (c) 2022 megvii-model. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from DETR3D (https://github.com/WangYueFt/detr3d)
# Copyright (c) 2021 Wang, Yue
# ------------------------------------------------------------------------
# Modified from mmdetection3d (https://github.com/open-mmlab/mmdetection3d)
# Copyright (c) OpenMMLab. All rights reserved.
# ------------------------------------------------------------------------

import torch
import torch.nn.functional as F
from einops import rearrange
from mmcv.runner import force_fp32, auto_fp16
from mmdet.models import DETECTORS
from mmdet3d.core import bbox3d2result
from mmdet3d.models.detectors.mvx_two_stage import MVXTwoStageDetector
from projects.mmdet3d_plugin.models.utils.grid_mask import GridMask


@DETECTORS.register_module()
class VEDet(MVXTwoStageDetector):
    """Petr3D."""

    def __init__(self,
                 use_grid_mask=False,
                 use_gt_scale=False,
                 pts_voxel_layer=None,
                 pts_voxel_encoder=None,
                 pts_middle_encoder=None,
                 pts_fusion_layer=None,
                 img_backbone=None,
                 pts_backbone=None,
                 img_neck=None,
                 pts_neck=None,
                 pts_bbox_head=None,
                 img_roi_head=None,
                 img_rpn_head=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None,
                 gt_depth_sup=True):
        super(VEDet, self).__init__(pts_voxel_layer, pts_voxel_encoder, pts_middle_encoder, pts_fusion_layer,
                                    img_backbone, pts_backbone, img_neck, pts_neck, pts_bbox_head, img_roi_head,
                                    img_rpn_head, train_cfg, test_cfg, pretrained)
        self.grid_mask = GridMask(True, True, rotate=1, offset=False, ratio=0.5, mode=1, prob=0.7)
        self.use_grid_mask = use_grid_mask
        self.use_gt_scale = use_gt_scale
        self.gt_depth_sup = gt_depth_sup

    def extract_img_feat(self, img, img_metas):
        """Extract features of images."""
        # print(img[0].size())
        if isinstance(img, list):
            img = torch.stack(img, dim=0)

        B = img.size(0)
        if img is not None:
            input_shape = img.shape[-2:]
            # update real input shape of each single img
            for img_meta in img_metas:
                img_meta.update(input_shape=input_shape)
            if img.dim() == 5:
                if img.size(0) == 1 and img.size(1) != 1:
                    img.squeeze_()
                else:
                    B, N, C, H, W = img.size()
                    img = img.view(B * N, C, H, W)
            if self.use_grid_mask:
                img = self.grid_mask(img)
            img_feats = self.img_backbone(img)
            if isinstance(img_feats, dict):
                img_feats = list(img_feats.values())
        else:
            return None
        if self.with_img_neck:
            img_feats = self.img_neck(img_feats)
        img_feats_reshaped = []
        for img_feat in img_feats:
            BN, C, H, W = img_feat.size()
            img_feats_reshaped.append(img_feat.view(B, int(BN / B), C, H, W))
        return img_feats_reshaped

    @auto_fp16(apply_to=('img'), out_fp32=True)
    def extract_feat(self, img, img_metas):
        """Extract features from images and points."""
        img_feats = self.extract_img_feat(img, img_metas)
        return img_feats

    def forward_pts_train(self, pts_feats, gt_bboxes_3d, gt_labels_3d, gt_maps, img_metas):
        """Forward function for point cloud branch.
        Args:
            pts_feats (list[torch.Tensor]): Features of point cloud branch
            gt_bboxes_3d (list[:obj:`BaseInstance3DBoxes`]): Ground truth
                boxes for each sample.
            gt_labels_3d (list[torch.Tensor]): Ground truth labels for
                boxes of each sampole
            img_metas (list[dict]): Meta information of samples.
            gt_bboxes_ignore (list[torch.Tensor], optional): Ground truth
                boxes to be ignored. Defaults to None.
        Returns:
            dict: Losses of each branch.
        """
        outs = self.pts_bbox_head(pts_feats, img_metas)
        loss_inputs = [gt_bboxes_3d, gt_labels_3d, gt_maps, outs]
        losses = self.pts_bbox_head.loss(*loss_inputs)

        return losses

    @force_fp32(apply_to=('img', 'points'))
    def forward(self, return_loss=True, **kwargs):
        """Calls either forward_train or forward_test depending on whether
        return_loss=True.
        Note this setting will change the expected inputs. When
        `return_loss=True`, img and img_metas are single-nested (i.e.
        torch.Tensor and list[dict]), and when `resturn_loss=False`, img and
        img_metas should be double nested (i.e.  list[torch.Tensor],
        list[list[dict]]), with the outer list indicating test time
        augmentations.
        """
        if return_loss:
            return self.forward_train(**kwargs)
        else:
            return self.forward_test(**kwargs)

    def forward_train(self, img_metas=None, gt_bboxes_3d=None, gt_labels_3d=None, maps=None, img=None):
        """Forward training function.
        Args:
            points (list[torch.Tensor], optional): Points of each sample.
                Defaults to None.
            img_metas (list[dict], optional): Meta information of each sample.
                Defaults to None.
            gt_bboxes_3d (list[:obj:`BaseInstance3DBoxes`], optional):
                Ground truth 3D boxes. Defaults to None.
            gt_labels_3d (list[torch.Tensor], optional): Ground truth labels
                of 3D boxes. Defaults to None.
            gt_labels (list[torch.Tensor], optional): Ground truth labels
                of 2D boxes in images. Defaults to None.
            gt_bboxes (list[torch.Tensor], optional): Ground truth 2D boxes in
                images. Defaults to None.
            img (torch.Tensor optional): Images of each sample with shape
                (N, C, H, W). Defaults to None.
            proposals ([list[torch.Tensor], optional): Predicted proposals
                used for training Fast RCNN. Defaults to None.
            gt_bboxes_ignore (list[torch.Tensor], optional): Ground truth
                2D boxes in images to be ignored. Defaults to None.
        Returns:
            dict: Losses of different branches.
        """

        img_feats = self.extract_feat(img=img, img_metas=img_metas)

        losses = dict()
        losses_pts = self.forward_pts_train(img_feats, gt_bboxes_3d, gt_labels_3d, maps, img_metas)
        losses.update(losses_pts)
        return losses

    def forward_test(self, img_metas, img=None, gt_map=None, **kwargs):
        for var, name in [(img_metas, 'img_metas')]:
            if not isinstance(var, list):
                raise TypeError('{} must be a list, but got {}'.format(name, type(var)))
        img = [img] if img is None else img
        return self.simple_test(img_metas[0], img[0], gt_map[0] if gt_map is not None else None, **kwargs)

    def simple_test_pts(self, x, img_metas, gt_map=None, rescale=False):
        """Test function of point cloud branch."""
        outs = self.pts_bbox_head(x, img_metas)
        results = dict()
        if outs.get('all_cls_scores', None) is not None:
            bbox_list = self.pts_bbox_head.get_bboxes(outs, img_metas, rescale=rescale)
            bbox_results = [bbox3d2result(bboxes, scores, labels) for bboxes, scores, labels in bbox_list]
            results['bbox_results'] = bbox_results

        if gt_map is not None:
            seg_results = self.compute_seg_iou(outs)
            results['seg_results'] = seg_results

        return results

    def simple_test(self, img_metas, img=None, gt_map=None, rescale=False):
        """Test function without augmentaiton."""
        img_feats = self.extract_feat(img=img, img_metas=img_metas)

        results_list = [dict() for i in range(len(img_metas))]
        results = self.simple_test_pts(img_feats, img_metas, gt_map, rescale=rescale)
        if 'bbox_results' in results:
            for result_dict, pts_bbox in zip(results_list, results['bbox_results']):
                result_dict['pts_bbox'] = pts_bbox

        return results_list

    @torch.no_grad()
    def compute_seg_iou(self, outs):
        lane_preds = outs['all_seg_preds'][-1].squeeze(0)  #[B,N,H,W]

        pred_maps = lane_preds.view(256, 3, 16, 16)

        f_lane = rearrange(pred_maps, '(h w) c h1 w2 -> c (h h1) (w w2)', h=16, w=16)
        f_lane = f_lane.sigmoid()
        f_lane[f_lane >= 0.5] = 1
        f_lane[f_lane < 0.5] = 0

        f_lane = f_lane.view(3, -1)
        gt_map = gt_map.view(3, -1)

        ret_iou = IOU(f_lane, gt_map).cpu()
        return ret_iou

    def aug_test_pts(self, feats, img_metas, rescale=False):
        feats_list = []
        for j in range(len(feats[0])):
            feats_list_level = []
            for i in range(len(feats)):
                feats_list_level.append(feats[i][j])
            feats_list.append(torch.stack(feats_list_level, -1).mean(-1))
        outs = self.pts_bbox_head(feats_list, img_metas)
        bbox_list = self.pts_bbox_head.get_bboxes(outs, img_metas, rescale=rescale)
        bbox_results = [bbox3d2result(bboxes, scores, labels) for bboxes, scores, labels in bbox_list]
        return bbox_results

    def aug_test(self, img_metas, imgs=None, rescale=False):
        """Test function with augmentaiton."""
        img_feats = self.extract_feats(img_metas, imgs)
        img_metas = img_metas[0]
        bbox_list = [dict() for i in range(len(img_metas))]
        bbox_pts = self.aug_test_pts(img_feats, img_metas, rescale)
        for result_dict, pts_bbox in zip(bbox_list, bbox_pts):
            result_dict['pts_bbox'] = pts_bbox
        return bbox_list


def IOU(intputs, targets):
    numerator = 2 * (intputs * targets).sum(dim=1)
    denominator = intputs.sum(dim=1) + targets.sum(dim=1)
    loss = (numerator + 0.01) / (denominator + 0.01)
    return loss
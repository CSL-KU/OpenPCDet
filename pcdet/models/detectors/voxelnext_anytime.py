from .anytime_template_v1 import AnytimeTemplateV1
import torch
import time

class VoxelNeXtAnytime(AnytimeTemplateV1):
    def __init__(self, model_cfg, num_class, dataset):
        super().__init__(model_cfg=model_cfg, num_class=num_class, dataset=dataset)

        self.vfe, self.backbone_3d, self.dense_head = self.module_list
        self.update_time_dict( {
                'VFE': [],
                'Sched': [],
                'Backbone3D':[],
                'VoxelHead': [],
                'Projection': []})

    def forward(self, batch_dict):
        self.projection_init(batch_dict)

        self.measure_time_start('VFE')
        batch_dict = self.vfe(batch_dict, model=self)
        self.measure_time_end('VFE')

        self.measure_time_start('Backbone3D')
        batch_dict = self.backbone_3d(batch_dict)
        self.measure_time_end('Backbone3D')

        self.measure_time_start('VoxelHead')
        batch_dict = self.dense_head(batch_dict)
        self.measure_time_end('VoxelHead')

        if self.training:
            loss, tb_dict, disp_dict = self.get_training_loss()

            ret_dict = {
                'loss': loss
            }
            return ret_dict, tb_dict, disp_dict
        else:
            self.measure_time_start('Projection')
            batch_dict = self.projection(batch_dict)
            self.measure_time_end('Projection')

            return batch_dict

    def calibrate(self):
        super().calibrate("calib_raw_data_voxelnext.json")
        return None

from .detector3d_template import Detector3DTemplate
import torch
import numpy as np
import numba
import time
import onnx
import onnxruntime as ort
import onnx_graphsurgeon as gs
import os
import struct
import sys
#import torch_tensorrt
#from ..model_utils.tensorrt_utils.trtwrapper import TRTWrapper

class OptimizedFwdPipeline(torch.nn.Module):
    def __init__(self, backbone_3d, map_to_bev, backbone_2d, dense_head):
        super().__init__()
        self.backbone_3d = backbone_3d
        self.map_to_bev = map_to_bev
        self.backbone_2d = backbone_2d
        self.dense_head = dense_head

    def forward(self,
            voxel_feat : torch.Tensor, 
            set_voxel_inds_tensor_shift_0 : torch.Tensor,
            set_voxel_inds_tensor_shift_1 : torch.Tensor,
            set_voxel_masks_tensor_shift_0 : torch.Tensor, 
            set_voxel_masks_tensor_shift_1: torch.Tensor,
            pos_embed_tensor : torch.Tensor,
            voxel_coords : torch.Tensor) -> torch.Tensor:
        output = self.backbone_3d(
                voxel_feat, set_voxel_inds_tensor_shift_0, 
                set_voxel_inds_tensor_shift_1, set_voxel_masks_tensor_shift_0,
                set_voxel_masks_tensor_shift_1, pos_embed_tensor)
        spatial_features = self.map_to_bev(1, output, voxel_coords)
        spatial_features_2d = self.backbone_2d(spatial_features)
        hm, center, center_z, dim, rot, vel, iou = \
                self.dense_head.forward_up_to_topk(spatial_features_2d)
        return hm, center, center_z, dim, rot, vel, iou

# Optimized with onnxruntime and torchscript
class DSVT_CenterHead_Opt(Detector3DTemplate):
    def __init__(self, model_cfg, num_class, dataset):
        super().__init__(model_cfg=model_cfg, num_class=num_class, dataset=dataset)
        torch.backends.cudnn.benchmark = True
        if torch.backends.cudnn.benchmark:
            torch.backends.cudnn.benchmark_limit = 0
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
        torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False

        torch.cuda.manual_seed(0)
        self.module_list = self.build_networks()

        self.is_voxel_enc=True
        self.vfe, self.backbone_3d, self.map_to_bev, self.backbone_2d, \
                self.dense_head = self.module_list

        self.update_time_dict( {
            'VFE-gen-pillars' : [],
            'VFE-nn' : [],
            'Backbone3D-IL': [],
            'FusedOps':[],
            #'MapToBEV': [],
            #'Backbone2D': [],
            #'CenterHead-Pre': [],
            #'CenterHead-Post': [],
            'CenterHead-Topk': [],
            'CenterHead-GenBox': [],
            #'CenterHead': [],
            })

        #self.inf_stream = torch.cuda.Stream()
        self.ort_out_sizes = None

    def forward(self, batch_dict):
        #with torch.cuda.stream(self.inf_stream):
        self.measure_time_start('VFE-gen-pillars')
        batch_dict = self.vfe.forward_gen_pillars(batch_dict)
        self.measure_time_end('VFE-gen-pillars')

        self.measure_time_start('VFE-nn')
        batch_dict = self.vfe.forward_nn(batch_dict)
        self.measure_time_end('VFE-nn')

        self.measure_time_start('Backbone3D-IL')
        fwd_data = self.backbone_3d.get_voxel_info(batch_dict['voxel_features'],
                batch_dict['voxel_coords'])
        self.measure_time_end('Backbone3D-IL')

        if self.ort_out_sizes == None:
            self.optimize(fwd_data)

        self.measure_time_start('FusedOps')
        # ONNX
        ro = ort.RunOptions()
        ro.add_run_config_entry("disable_synchronize_execution_providers", "1")
        outputs = [torch.empty(sz, device='cuda', dtype=torch.float) \
                for sz in self.ort_out_sizes]
        io_binding = self.get_iobinding(self.ort_session, fwd_data, outputs)
        self.ort_session.run_with_iobinding(io_binding, run_options=ro)
        out_dict = self.dense_head.convert_out_to_batch_dict(outputs)
        batch_dict["pred_dicts"] = [out_dict]
        torch.cuda.synchronize()
        self.measure_time_end('FusedOps')

        #TODO , use the optimized cuda code for the rest available in autoware
        self.measure_time_start('CenterHead-Topk')
        batch_dict = self.dense_head.forward_topk(batch_dict)
        self.measure_time_end('CenterHead-Topk')
        self.measure_time_start('CenterHead-GenBox')
        batch_dict = self.dense_head.forward_genbox(batch_dict)
        self.measure_time_end('CenterHead-GenBox')

        if self.training:
            loss, tb_dict, disp_dict = self.get_training_loss()

            ret_dict = {
                    'loss': loss
                    }
            return ret_dict, tb_dict, disp_dict
        else:
            # let the hooks of parent class handle this
            return batch_dict

    def optimize(self, fwd_data):
        optimize_start = time.time()

        input_names = [
                'voxel_feat',
                'set_voxel_inds_tensor_shift_0',
                'set_voxel_inds_tensor_shift_1',
                'set_voxel_masks_tensor_shift_0',
                'set_voxel_masks_tensor_shift_1',
                'pos_embed_tensor',
                'voxel_coords'
                ]

        output_names = self.dense_head.ordered_outp_names()
        print('Fused operations output names:', output_names)

        opt_fwd = OptimizedFwdPipeline(self.backbone_3d, self.map_to_bev,
                self.backbone_2d, self.dense_head)
        opt_fwd.eval()
        eager_outputs = opt_fwd(*fwd_data)

        generated_onnx=False
        base_dir = "./deploy_files"
        onnx_path = f"{base_dir}/dsvt.onnx"
        if not os.path.exists(onnx_path):
            dynamic_axes = {
                "voxel_feat": {
                    0: "voxel_number",
                },
                "set_voxel_inds_tensor_shift_0": {
                    1: "set_number_shift_0",
                },
                "set_voxel_inds_tensor_shift_1": {
                    1: "set_number_shift_1",
                },
                "set_voxel_masks_tensor_shift_0": {
                    1: "set_number_shift_0",
                },
                "set_voxel_masks_tensor_shift_1": {
                    1: "set_number_shift_1",
                },
                "pos_embed_tensor": {
                    2: "voxel_number",
                },
                "voxel_coords": {
                    0: "voxel_number",
                }
            }
            
            torch.onnx.export(
                    opt_fwd,
                    fwd_data,
                    onnx_path, input_names=input_names,
                    output_names=output_names, dynamic_axes=dynamic_axes,
                    opset_version=17,
                    custom_opsets={"kucsl": 17}
            )
            #graph = gs.import_onnx(onnx.load(onnx_path))
            #graph.fold_constants()
            #graph.cleanup(remove_unused_graph_inputs=True).toposort()
            #mdl = onnx.shape_inference.infer_shapes(gs.export_onnx(graph))
            #onnx.save(mdl, onnx_path)
            generated_onnx=True

        self.ort_out_sizes = [tuple(out.shape) for out in eager_outputs]
        print('Optimized forward pipeline output sizes:\n', self.ort_out_sizes)

        so = ort.SessionOptions()
        so.log_severity_level = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        so.optimized_model_filepath = f"{base_dir}/dsvt_optimized.onnx" # speeds up initialization
        cuda_conf = {
                'device_id': torch.cuda.current_device(),
                'user_compute_stream': str(torch.cuda.current_stream().cuda_stream),
                'arena_extend_strategy': 'kNextPowerOfTwo',
                #'gpu_mem_limit': 2 * 1024 * 1024 * 1024,
                'cudnn_conv_algo_search': 'HEURISTIC',  #'EXHAUSTIVE',
                'do_copy_in_default_stream': True,
                'use_tf32': 1,
        }

        EP_list= [#('TensorrtExecutionProvider', tensorrt_conf),
                ('CUDAExecutionProvider', cuda_conf),
                'CPUExecutionProvider']

        if os.path.exists(so.optimized_model_filepath):
            onnx_path=so.optimized_model_filepath
        self.ort_session = ort.InferenceSession(onnx_path, providers=EP_list, sess_options=so)
        self.ort_session.enable_fallback()
        outputs = [torch.empty(sz, device='cuda', dtype=torch.float) \
                for sz in self.ort_out_sizes]
        io_binding = self.get_iobinding(self.ort_session, fwd_data, outputs)
        self.ort_session.run_with_iobinding(io_binding)
        io_binding.copy_outputs_to_cpu()[0] # to sync

        optimize_end = time.time()
        print(f'Optimization took {optimize_end-optimize_start} seconds.')
        if generated_onnx:
            print('Optimization done, please run again.')
            sys.exit(0)

    def get_iobinding(self, ort_session, inp_tensors, outp_tensors):
        typedict = {torch.float: np.float32,
                torch.bool: np.bool_,
                torch.int: np.int32,
                torch.long: np.int64}
        io_binding = ort_session.io_binding()
        for inp, tensor in zip(ort_session.get_inputs(), inp_tensors):
            io_binding.bind_input(
                    name=inp.name,
                    device_type='cuda',
                    device_id=0,
                    element_type=typedict[tensor.dtype],
                    shape=tuple(tensor.shape),
                    buffer_ptr=tensor.data_ptr()
                    )
        for outp, tensor in zip(ort_session.get_outputs(), outp_tensors):
            io_binding.bind_output(
                    name=outp.name,
                    device_type='cuda',
                    device_id=0,
                    element_type=typedict[tensor.dtype],
                    shape=tuple(tensor.shape),
                    buffer_ptr=tensor.data_ptr()
                    )

        return io_binding


    def get_training_loss(self):
        disp_dict = {}

        loss_rpn, tb_dict = self.dense_head.get_loss()
        tb_dict = {
                'loss_rpn': loss_rpn.item(),
                **tb_dict
                }

        loss = loss_rpn
        return loss, tb_dict, disp_dict

    def post_processing_pre(self, batch_dict):
        return (batch_dict,)

    def post_processing_post(self, pp_args):
        batch_dict = pp_args[0]
        post_process_cfg = self.model_cfg.POST_PROCESSING
        batch_size = batch_dict['batch_size']
        final_pred_dict = batch_dict['final_box_dicts']
        recall_dict = {}
        for index in range(batch_size):
            pred_boxes = final_pred_dict[index]['pred_boxes']

            recall_dict = self.generate_recall_record(
                    box_preds=pred_boxes.cuda(),
                    recall_dict=recall_dict, batch_index=index, data_dict=batch_dict,
                    thresh_list=post_process_cfg.RECALL_THRESH_LIST
                    )

        return final_pred_dict, recall_dict

    def calibrate(self, batch_size=1):
        return super().calibrate(1)

############SOME CODE HERE TO USE LATER IF NEEDED ##################
#        trt_path = self.model_cfg.BACKBONE_3D.trt_engine
#        self.compiled_bb3d = TRTWrapper(trt_path, input_names, output_names)
#        print('Outputs after trt inference:')
#        inputs_dict = {'voxel_feat': vinfo[0],
#                'set_voxel_inds_tensor_shift_0': vinfo[1],
#                'set_voxel_inds_tensor_shift_1': vinfo[2],
#                'set_voxel_masks_tensor_shift_0': vinfo[3],
#                'set_voxel_masks_tensor_shift_1': vinfo[4],
#               'pos_embed_tensor' : vinfo[5]}
#        out = self.compiled_bb3d(inputs_dict)['output']
#        print(out.size(), out)

#            inp1, inp2, inp3, inp4, inp5, inp6 = vinfo
#            torch._dynamo.mark_dynamic(inp1, 0, min=3000, max=30000)
#            torch._dynamo.mark_dynamic(inp2, 1, min=60, max=400)
#            torch._dynamo.mark_dynamic(inp3, 1, min=60, max=400)
#            torch._dynamo.mark_dynamic(inp4, 1, min=60, max=400)
#            torch._dynamo.mark_dynamic(inp5, 1, min=60, max=400)
#            torch._dynamo.mark_dynamic(inp6, 2, min=3000, max=30000)
#            export_options = torch.onnx.ExportOptions(dynamic_shapes=True)
#            torch.onnx.enable_fake_mode()
#            onnx_program = torch.onnx.dynamo_export(self.backbone_3d,
#                    inp1, inp2, inp3, inp4, inp5, inp6, export_options=export_options)
#            onnx_program.save(onnx_path)

#            sm = torch.jit.script(self.backbone_3d, example_inputs=[vinfo])
#            output = sm(*vinfo)
#            print('Outputs after torchscript inference:')
#            print(output.size(), output)

         ####TensorRT ONNX conf
#        def get_shapes_str(d1, d2, d3):
#            return f'voxel_feat:{d1}x128,set_voxel_inds_tensor_shift_0:2x{d2}x90,' \
#        f'set_voxel_inds_tensor_shift_1:2x{d3}x90,set_voxel_masks_tensor_shift_0:2x{d2}x90,' \
#        f'set_voxel_masks_tensor_shift_1:2x{d3}x36,pos_embed_tensor:4x2x{d1}x128'
#        
#        tensorrt_conf = {
#                'device_id': torch.cuda.current_device(),
#                "user_compute_stream": str(torch.cuda.current_stream().cuda_stream),
#                #'trt_max_workspace_size': 2147483648,
#                'trt_profile_min_shapes': get_shapes_str(3000, 60, 60),
#                'trt_profile_opt_shapes': get_shapes_str(11788,190,205),
#                'trt_profile_max_shapes': get_shapes_str(30000, 400, 400),
#                'trt_fp16_enable': False,
#                'trt_layer_norm_fp32_fallback': True,
#        }

#                #TensorRT
#                inputs_dict = {'voxel_feat': vinfo[0],
#                        'set_voxel_inds_tensor_shift_0': vinfo[1],
#                        'set_voxel_inds_tensor_shift_1': vinfo[2],
#                        'set_voxel_masks_tensor_shift_0': vinfo[3],
#                        'set_voxel_masks_tensor_shift_1': vinfo[4],
#                        'pos_embed_tensor' : vinfo[5]}
#                output = self.compiled_bb3d(inputs_dict)['output']



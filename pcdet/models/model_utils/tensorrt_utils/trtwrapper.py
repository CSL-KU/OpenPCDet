# Copyright (c) OpenMMLab. All rights reserved.
from typing import Any, Dict, Optional, Sequence, Union

import tensorrt as trt
import torch

from .utils import load_trt_engine, torch_device_from_trt, torch_dtype_from_trt


class TRTWrapper(torch.nn.Module):
    """TensorRT engine wrapper for inference.

    Args:
        engine (tensorrt.ICudaEngine): TensorRT engine to wrap.
        output_names (Sequence[str] | None): Names of model outputs  in order.
            Defaults to `None` and the wrapper will load the output names from
            model.

    Note:
        If the engine is converted from onnx model. The input_names and
        output_names should be the same as onnx model.

    Examples:
        >>> from mmdeploy.backend.tensorrt import TRTWrapper
        >>> engine_file = 'resnet.engine'
        >>> model = TRTWrapper(engine_file)
        >>> inputs = dict(input=torch.randn(1, 3, 224, 224))
        >>> outputs = model(inputs)
        >>> print(outputs)
    """

    def __init__(
            self,
            engine: Union[str, trt.ICudaEngine],
            input_names: Sequence[str],
            output_names: Sequence[str],
    ):
        super().__init__()
        # NOTE use TensorRT default one
        # load_tensorrt_plugin()
        trt.init_libnvinfer_plugins(None, '')
        self.engine = engine
        if isinstance(self.engine, str):
            self.engine = load_trt_engine(engine)

        if not isinstance(self.engine, trt.ICudaEngine):
            raise TypeError(f'`engine` should be str or trt.ICudaEngine, \
                but given: {type(self.engine)}')

        self._input_names = input_names
        self._output_names = output_names

        # self._register_state_dict_hook(TRTWrapper.__on_state_dict)
        self.context = self.engine.create_execution_context()

    def forward(
            self,
            inputs: Dict[str, torch.Tensor],
            output_sz: tuple[int] = None
    ) -> Dict[str, torch.Tensor]:
        """Run forward inference.

        Args:
            inputs (Dict[str, torch.Tensor]): The input name and tensor pairs.

        Return:
            Dict[str, torch.Tensor]: The output name and tensor pairs.
        """

        profile_id = 0
        for input_name, input_tensor in inputs.items():
            # check if input shape is valid
            profile = self.engine.get_tensor_profile_shape(input_name, profile_id)
            assert input_tensor.dim() == len(
                profile[0]), 'Input dim is different from engine profile.'
            for s_min, s_input, s_max in zip(eval(repr(profile[0])), input_tensor.shape,
                                             eval(repr(profile[2]))):
                assert s_min <= s_input <= s_max, \
                    f'Input shape of {input_name} should be between ' \
                    + f'{profile[0]} and {profile[2]}' \
                    + f' but get {tuple(input_tensor.shape)}.'

            self.context.set_input_shape(input_name, tuple(input_tensor.shape))
            self.context.set_tensor_address(input_name, input_tensor.data_ptr())

        assert self.context.all_binding_shapes_specified


        # create output tensors
        outputs = {}
        for output_name in self._output_names:
            dtype = torch_dtype_from_trt(self.engine.get_tensor_dtype(output_name))
            shape = eval(repr(self.context.get_tensor_shape(output_name)))

            if output_sz is not None:
                shape = output_sz
            output = torch.empty(size=shape, dtype=dtype, device='cuda').contiguous()
            self.context.set_tensor_address(output_name, output.data_ptr())
            outputs[output_name] = output

        self.__trt_execute() #bindings=bindings)

        return outputs

    def __trt_execute(self):
        """Run inference with TensorRT."""
        self.context.execute_async_v3(
            torch.cuda.current_stream().cuda_stream,
        )

import os
import gc
import json
import torch
import transformers
import torch.nn as nn

from tqdm import tqdm
from typing import List, Union
from safetensors.torch import save_file
from huggingface_hub import snapshot_download
from transformers.modeling_utils import shard_checkpoint

from quick.awq.modules.linear.quick import WQLinear_QUICK
from quick.awq.modules.linear.gemm import WQLinear_GEMM
from quick.awq.modules.linear.gemv import WQLinear_GEMV
from quick.awq.modules.linear.exllama import WQLinear_Exllama, exllama_post_init
from quick.awq.modules.linear.exllamav2 import WQLinear_ExllamaV2, exllamav2_post_init
from quick.awq.utils.module import (
    get_named_linears,
    set_op_by_name,
    exclude_layers_to_not_quantize,
)
from transformers import (
    AutoConfig,
    PreTrainedModel,
    PretrainedConfig,
    AutoProcessor,
    CLIPImageProcessor,
)
from accelerate.big_modeling import (
    init_empty_weights,
    load_checkpoint_and_dispatch,
)

from quick.awq.models._config import AwqConfig
from quick.awq.modules.act import ScaledActivation
from quick.awq.quantize.quantizer import AwqQuantizer
from quick.awq.utils.module import get_named_linears, set_op_by_name
from quick.awq.utils.fused_utils import get_attention_shapes
from quick.awq.modules.fused.cache import WindowedCache
from quick.awq.modules.fused.attn import RoPE, ALiBi
from quick.awq.quantize.quantizer import QuantAttentionFused

# Since we support different `AutoModelForxxx` from transformers
# we need to define a custom mapping dict as below:
TRANSFORMERS_AUTO_MAPPING_DICT = {
    "mpt": "AutoModelForCausalLM",
    "llama": "AutoModelForCausalLM",
    "opt": "AutoModelForCausalLM",
    "RefinedWeb": "AutoModelForCausalLM",
    "RefinedWebModel": "AutoModelForCausalLM",
    "falcon": "AutoModelForCausalLM",
    "bloom": "AutoModelForCausalLM",
    "gptj": "AutoModelForCausalLM",
    "gpt_bigcode": "AutoModelForCausalLM",
    "mistral": "AutoModelForCausalLM",
    "mixtral": "AutoModelForCausalLM",
    "gpt_neox": "AutoModelForCausalLM",
    "aquila": "AutoModelForCausalLM",
    "Yi": "AutoModelForCausalLM",
    "qwen": "AutoModelForCausalLM",
    "baichuan": "AutoModelForCausalLM",
    "llava": "AutoModelForVision2Seq",
}


class BaseAWQForCausalLM(nn.Module):
    def __init__(
        self, model, model_type, is_quantized, config, quant_config, processor
    ):
        super().__init__()
        self.model: PreTrainedModel = model
        self.model_type: str = model_type
        self.is_quantized: bool = is_quantized
        self.search_result = None
        self.config: PretrainedConfig = config
        self.quant_config: AwqConfig = quant_config
        self.processor: CLIPImageProcessor = processor

    def to(self, device: str):
        return self.model.to(device)

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def generate(self, *args, **kwargs):
        with torch.inference_mode():
            return self.model.generate(*args, **kwargs)

    @torch.no_grad()
    def quantize(
        self,
        tokenizer=None,
        quant_config={},
        calib_data: Union[str, List[str]] = "pileval",
        split="train",
        text_column="text",
        duo_scaling=True,
        modules_to_not_convert=None,
        export_compatible=False,
    ):
        self.quant_config: AwqConfig = AwqConfig.from_dict(quant_config)

        self.quantizer = AwqQuantizer(
            self,
            self.model,
            tokenizer,
            self.quant_config.w_bit,
            self.quant_config.q_group_size,
            self.quant_config.version,
            calib_data,
            split,
            text_column,
            duo_scaling,
            modules_to_not_convert=modules_to_not_convert,
            export_compatible=export_compatible,
        )
        self.quantizer.quantize()

        self.is_quantized = True

    @torch.no_grad()
    def pack(self):
        """
        A utility function for the following scenario. Note that save_quantized will
        overwrite existing weights if you use the same quant_path.

        model.quantize(
            tokenizer,
            quant_config=quant_config,
            export_compatible=True
        )
        model.save_quantized(...)  # produces GGUF/other compat weights
        model.pack(...) # makes the model CUDA compat
        model.save_quantized(...)  # produces CUDA compat weights
        """
        self.quantizer.pack()

    @staticmethod
    def fuse_layers(model):
        pass

    def save_quantized(self, save_dir, safetensors=True, shard_size="10GB"):
        save_dir = save_dir[:-1] if save_dir[-1] == "/" else save_dir

        # Save model
        class EmptyModule(nn.Module):
            def __init__(self):
                super(EmptyModule, self).__init__()

            def forward(self, x):
                return x

        # Save model and config files with empty state dict
        self.model.config.quantization_config = self.quant_config.to_transformers_dict()
        self.model.save_pretrained(save_dir, state_dict=EmptyModule().state_dict())
        self.quant_config.save_pretrained(save_dir)

        # Vision transformers have a processor
        if self.processor is not None:
            self.processor.save_pretrained(save_dir)

        # Remove empty state dict
        default_paths = [
            f"{save_dir}/model.safetensors",
            f"{save_dir}/pytorch_model.bin",
        ]
        for path in default_paths:
            if os.path.exists(path):
                os.remove(path)

        # model_name has no extension, add it when saving state_dict
        model_name = "model.safetensors" if safetensors else "pytorch_model.bin"

        # shard checkpoint into chunks (10GB default)
        shards, index = shard_checkpoint(
            self.model.state_dict(), max_shard_size=shard_size, weights_name=model_name
        )

        for shard_file, shard in shards.items():
            if safetensors:
                # safetensors must be in the same memory, so we duplicate and use contiguous memory
                shard = {k: v.clone().contiguous() for k, v in shard.items()}
                save_file(
                    shard, os.path.join(save_dir, shard_file), metadata={"format": "pt"}
                )
            else:
                torch.save(shard, os.path.join(save_dir, shard_file))

        # save shard index
        if index is not None:
            with open(f"{save_dir}/{model_name}.index.json", "w+") as file:
                file.write(json.dumps(index, indent=4))

    @classmethod
    def from_pretrained(
        self,
        model_path,
        model_type,
        torch_dtype: torch.dtype = torch.float16,
        trust_remote_code=True,
        safetensors=False,
        device_map=None,
        **model_init_kwargs,
    ):
        # Get weights path and quant config
        model_weights_path, config, quant_config = self._load_config(
            self, model_path, "", safetensors, trust_remote_code=trust_remote_code
        )

        target_cls_name = TRANSFORMERS_AUTO_MAPPING_DICT[config.model_type]
        target_cls = getattr(transformers, target_cls_name)

        processor = None
        if target_cls_name == "AutoModelForVision2Seq":
            processor = AutoProcessor.from_pretrained(model_weights_path)
            processor: CLIPImageProcessor = processor.image_processor

        # If not quantized, must load with AutoModelForCausalLM
        model = target_cls.from_pretrained(
            model_weights_path,
            trust_remote_code=trust_remote_code,
            torch_dtype=torch_dtype,
            use_safetensors=safetensors,
            device_map=device_map,
            **model_init_kwargs,
        )

        model.eval()

        return self(
            model,
            model_type,
            is_quantized=False,
            config=config,
            quant_config=quant_config,
            processor=processor,
        )

    @classmethod
    def from_quantized(
        self,
        model_path,
        model_type,
        model_filename="",
        max_new_tokens=None,
        torch_dtype=torch.float16,
        trust_remote_code=True,
        safetensors=True,
        is_quantized=True,
        fuse_layers=False,
        use_exllama=False,
        use_exllama_v2=False,
        version="GEMM",
        device_map="balanced",
        offload_folder=None,
        **config_kwargs,
    ):
        # [STEP 1-2] Load weights path and configs
        model_weights_path, config, quant_config = self._load_config(
            self,
            model_path,
            model_filename,
            safetensors,
            version,
            trust_remote_code,
            max_new_tokens=max_new_tokens,
            **config_kwargs,
        )

        target_cls_name = TRANSFORMERS_AUTO_MAPPING_DICT[config.model_type]
        target_cls = getattr(transformers, target_cls_name)

        # [STEP 3] Load model
        with init_empty_weights():
            model = target_cls.from_config(
                config=config,
                torch_dtype=torch_dtype,
                trust_remote_code=trust_remote_code,
            )

        # Prepare WQLinear layers, replace nn.Linear
        self._load_quantized_modules(
            self,
            model,
            quant_config,
            quant_config.version,
            use_exllama=use_exllama,
            use_exllama_v2=use_exllama_v2,
        )

        model.tie_weights()

        if quant_config.version == "QUICK":
            for i in range(len(model.model.layers)): 
                del model.model.layers[i].self_attn.rope
        
        # loads the weights into modules and distributes
        # across available devices automatically
        load_checkpoint_and_dispatch(
            model,
            checkpoint=model_weights_path,
            device_map=device_map,
            no_split_module_classes=[self.layer_type],
            offload_folder=offload_folder,
            dtype=torch_dtype,
        )
        
        # Dispath to devices
        if fuse_layers:
            self.fuse_layers(model)
        
        if use_exllama:
            # creates q4 handle
            print('use_exllama')
            model = exllama_post_init(model)
        elif use_exllama_v2:
            # creates q4 handle and allocates scratch spaces wrt max_input_len and
            # max_batch_size, which are hardcoded for now but might be worth interfacing
            print('use_exllama_v2')
            model = exllamav2_post_init(
                model,
                max_input_len=max_new_tokens,
                max_batch_size=int(os.getenv("AWQ_BATCH_SIZE", 1))
            )

        return self(
            model,
            model_type,
            is_quantized=is_quantized,
            config=config,
            quant_config=quant_config,
            processor=None,
        )

    def _load_config(
        self,
        model_path,
        model_filename,
        safetensors=True,
        version="GEMM",
        trust_remote_code=True,
        max_new_tokens=4096,
        **config_kwargs,
    ):
        # [STEP 1] Download model if path is not a directory
        if not os.path.isdir(model_path):
            ignore_patterns = ["*msgpack*", "*h5*", "optimizer.pt"]
            if safetensors:
                ignore_patterns.extend(["*.pt*", "*.bin*", "consolidated*"])
            else:
                ignore_patterns.append("*.safetensors*")

            model_path = snapshot_download(model_path, ignore_patterns=ignore_patterns)

        if model_filename != "":
            model_weights_path = model_path + f"/{model_filename}"
        else:
            model_weights_path = model_path

        # [STEP 2] Load config and set sequence length
        # TODO: Create BaseAWQConfig class
        quant_config = AwqConfig.from_pretrained(model_path)

        # Load model config and set max generation length
        if max_new_tokens is None and hasattr(self, "max_new_tokens_key"):
            config = AutoConfig.from_pretrained(
                model_path, trust_remote_code=trust_remote_code, **config_kwargs
            )
            config.max_new_tokens = getattr(config, self.max_new_tokens_key, 2048)
            # To add the generate support for Multi-modal models as well
            if hasattr(config, "text_config"):
                config.text_config.max_new_tokens = getattr(
                    config, self.max_new_tokens_key, 2048
                )
        else:
            max_new_tokens = 2048 if max_new_tokens is None else max_new_tokens
            config = AutoConfig.from_pretrained(
                model_path, trust_remote_code=trust_remote_code, **config_kwargs
            )
            config.max_new_tokens = max_new_tokens

        return model_weights_path, config, quant_config

    def _load_quantized_modules(
        self, model, quant_config, version, use_exllama, use_exllama_v2
    ):
        # Real quantization of weights
        assert quant_config.zero_point, "We only support zero_point quantization now."
        assert not (
            version == "GEMV" and (use_exllama or use_exllama_v2)
        ), "Exllama kernels only support GEMM version."
        print("Kernel Version: ", version)
        # Get blocks of model
        layers = self.get_model_layers(model)

        for i in tqdm(range(len(layers)), desc="Replacing layers..."):
            layer = layers[i]

            # Replace activation functions
            self._scale_activations(self, layer)

            if version == "QUICK":
                weights = torch.cat([layer.self_attn.q_proj.weight, layer.self_attn.k_proj.weight, layer.self_attn.v_proj.weight], dim=0)
                bias = torch.cat([layer.self_attn.q_proj.bias, layer.self_attn.k_proj.bias, layer.self_attn.v_proj.bias], dim=0) if layer.self_attn.q_proj.bias is not None else None
                qkv_layer = nn.Linear(layer.self_attn.q_proj.in_features, layer.self_attn.q_proj.out_features+layer.self_attn.k_proj.out_features+layer.self_attn.v_proj.out_features, layer.self_attn.q_proj.bias is not None, layer.self_attn.q_proj.weight.device)

                qkv_layer.weight.data = weights
                qkv_layer.bias = bias
                attn = QuantAttentionFused(hidden_size=model.config.hidden_size, n_heads=model.config.num_attention_heads, qkv_layer=qkv_layer, o_proj=layer.self_attn.o_proj, n_kv_heads=model.config.num_key_value_heads,
                                    dev=layer.self_attn.q_proj.weight.device, max_seq_len=model.config.max_new_tokens, rope_theta=model.config.rope_theta) #AttributeError: 'LlamaConfig' object has no attribute 'max_new_tokens'
                set_op_by_name(layer, 'self_attn', attn)
            
            # Get every linear layer in a block
            named_linears = get_named_linears(layer)

            # Filter out the linear layers we don't want to exclude
            named_linears = exclude_layers_to_not_quantize(
                named_linears, quant_config.modules_to_not_convert
            )
            gpu_A100 = False
            gpu_device = torch.cuda.get_device_name()
            if 'A100' in gpu_device and 'A1000' not in gpu_device:
                gpu_A100 = True
                
            # Replace nn.Linear with WQLinear
            for name, module in named_linears.items():
                if version == "QUICK":
                    q_linear_module = WQLinear_QUICK
                elif use_exllama:
                    q_linear_module = WQLinear_Exllama
                elif use_exllama_v2:
                    q_linear_module = WQLinear_ExllamaV2
                elif version == "GEMM":
                    q_linear_module = WQLinear_GEMM
                elif version == "GEMV":
                    q_linear_module = WQLinear_GEMV

                if version == "QUICK" and gpu_A100:
                    q_linear = q_linear_module.from_linear(
                        module, quant_config.w_bit, quant_config.q_group_size, True, k_split_1=16, k_split_2=16
                    )
                else:
                    q_linear = q_linear_module.from_linear(
                        module, quant_config.w_bit, quant_config.q_group_size, True
                    )
                q_linear.to(next(layer.parameters()).device)
                set_op_by_name(layer, name, q_linear)

            torch.cuda.empty_cache()
            gc.collect()

    @staticmethod
    def _scale_activations(self, layer):
        scale_dict = self.get_act_for_scaling(layer)

        if scale_dict["is_scalable"]:
            if not isinstance(scale_dict["scale_layer"], ScaledActivation):
                param = next(layer.parameters())

                # get activation scale
                scale_like = torch.ones(
                    scale_dict["scale_shape"], dtype=param.dtype, device=param.device
                )

                # scale activation
                scaled_act = ScaledActivation(scale_dict["scale_layer"], scale_like)
                set_op_by_name(layer, scale_dict["scale_name"], scaled_act)
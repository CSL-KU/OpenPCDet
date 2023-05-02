if [ -z $1 ]; then
	printf "Give cmd line arg, profile, methods, or slices"
	exit
fi

PROF_CMD=""
if [ $1 == 'profile' ]; then
	PROF_CMD="./nsys profile -w true \
		--trace cuda,nvtx \
		--process-scope=process-tree"
	# osrt and cudnn doesn't work :(
	#--sampling-trigger=timer,sched,cuda \

	# if want to trace stage2 only
	#NUM_SAMPLES=5
	#ARGS="$ARGS -c nvtx \
	#	--capture-range-end=repeat-shutdown:$NUM_SAMPLES \
	#	-p RPNstage2@* \
	#	-e NSYS_NVTX_PROFILER_REGISTER_ONLY=0 \
	#	--sampling-frequency=50000 --cuda-memory-usage=true"
fi

# Imprecise model
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_pp_multihead_imprecise.yaml"
#CKPT_FILE="../models/cbgs_pp_multihead_imprecise.pth"

#SECOND CBGS
#CFG_FILE="./cfgs/nuscenes_models/cbgs_second_multihead.yaml"
#CKPT_FILE="../models/cbgs_second_multihead_nds6229_updated.pth"

# PointPillars Single Head
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_pp_singlehead.yaml"
#CKPT_FILE="../models/cbgs_dyn_pp_singlehead/default/ckpt/checkpoint_epoch_20.pth"

#PointPillars Multihead
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_pp_multihead_3br.yaml"
#CKPT_FILE="../models/pp_multihead_nds5823_updated.pth"

# Centerpoint-pointpillar
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_pp_centerpoint.yaml"
#CKPT_FILE="../models/cbgs_dyn_pp_centerpoint_12_5_data.pth"

# Centerpoint-pointpillar-anytime
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_pp_centerpoint_anytime_16x16.yaml"
#CKPT_FILE="../models/cbgs_dyn_pp_centerpoint_anytime_16x16_12_5_data.pth"

# Centerpoint-voxel01
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_voxel01_res3d_centerpoint.yaml"
#CKPT_FILE="../models/cbgs_dyn_voxel01_centerpoint.pth"

# Centerpoint-voxel01-anytime
#CFG_FILE="./cfgs/nuscenes_models/cbgs_dyn_voxel01_res3d_centerpoint_anytime_16x16.yaml"
#CKPT_FILE="../models/cbgs_voxel01_centerpoint_anytime_16x16.pth"

# Centerpoint-voxel0075
# NDS:     0.5522 End-to-end,305.03,411.82,446.76,466.24,469.26,20.38
#CFG_FILE="./cfgs/nuscenes_models/cbgs_voxel0075_res3d_centerpoint.yaml"
#CKPT_FILE="../models/cbgs_voxel0075_centerpoint_nds_6648.pth"

# Centerpoint-KITTI-voxel
#CFG_FILE="./cfgs/kitti_models/centerpoint.yaml"
#CKPT_FILE="../models/centerpoint_kitti.pth"

# VoxelNeXt
#NDS:     0.5501 End-to-end,202.77,315.00,347.92,354.18,363.76,22.32
#CFG_FILE="./cfgs/nuscenes_models/cbgs_voxel0075_voxelnext.yaml"
CFG_FILE="./cfgs/nuscenes_models/cbgs_voxel0075_voxelnext_anytime.yaml"
CKPT_FILE="../models/voxelnext_nuscenes_kernel1.pth"

TASKSET="taskset -c 2,3"
export OMP_NUM_THREADS=2

#DATASET="nuscenes_dataset.yaml"
#DATASET="nuscenes_mini_dataset.yaml"
#ARG="s/_BASE_CONFIG_: cfgs\/dataset_configs.*$"
#ARG=$ARG"/_BASE_CONFIG_: cfgs\/dataset_configs\/$DATASET/g"
#sed -i "$ARG" $CFG_FILE

#CMD="nice --20 $PROF_CMD $TASKSET python test.py --cfg_file=$CFG_FILE \
#	--ckpt $CKPT_FILE --batch_size=1 --workers 0"
CMD="chrt -f 90 $PROF_CMD $TASKSET python test.py --cfg_file=$CFG_FILE \
	--ckpt $CKPT_FILE --batch_size=1 --workers 0"

#export CUBLAS_WORKSPACE_CONFIG=":4096:2"

set -x
if [ $1 == 'profile' ]; then
        export CUDA_LAUNCH_BLOCKING=1
        $CMD --set "MODEL.DEADLINE_SEC" $2
        export CUDA_LAUNCH_BLOCKING=0
elif [ $1 == 'methods' ]; then
	mv -f eval_dict_* backup
	OUT_DIR=exp_data_nsc
	mkdir -p $OUT_DIR
    

    CFG_FILES=( \
	"./cfgs/nuscenes_models/cbgs_voxel0075_voxelnext_anytime.yaml" \
	"./cfgs/nuscenes_models/cbgs_voxel0075_voxelnext.yaml")
#        "./cfgs/nuscenes_models/cbgs_dyn_voxel01_res3d_centerpoint_anytime_16x16.yaml" \
#        "./cfgs/nuscenes_models/cbgs_dyn_voxel01_res3d_centerpoint.yaml" \
#        "./cfgs/nuscenes_models/cbgs_dyn_voxel01_res3d_centerpoint_anytime_16x16.yaml" )
    CKPT_FILES=( \
	"../models/voxelnext_nuscenes_kernel1.pth"\
	"../models/voxelnext_nuscenes_kernel1.pth")
#        "../models/cbgs_voxel01_centerpoint_anytime_16x16.pth" \
#        "../models/cbgs_voxel01_centerpoint.pth" \
#        "../models/cbgs_voxel01_centerpoint_anytime_16x16.pth" )
    
    for m in ${!CFG_FILES[@]}
	do
        #if [ $m == 1 ]; then
        #    continue
        #fi
	CFG_FILE=${CFG_FILES[$m]}
	CKPT_FILE=${CKPT_FILES[$m]}
	#CMD="nice --20 $TASKSET python test.py --cfg_file=$CFG_FILE \
	#	--ckpt $CKPT_FILE --batch_size=1 --workers 0"
	CMD="chrt -f 90 $TASKSET python test.py --cfg_file=$CFG_FILE \
		--ckpt $CKPT_FILE --batch_size=1 --workers 0"
	for s in $(seq $2 $3 $4)
	do
		OUT_FILE=$OUT_DIR/eval_dict_m"$m"_d"$s".json
		if [ -f $OUT_FILE ]; then
			printf "Skipping $OUT_FILE test.\n"
		else
			$CMD --set "MODEL.DEADLINE_SEC" $s "MODEL.METHOD" $m
			# rename the output and move the corresponding directory
			mv -f eval_dict_*.json $OUT_DIR/eval_dict_m"$m"_d"$s".json
		fi
	done
	done
elif [ $1 == 'single' ]; then
        $CMD  --set "MODEL.DEADLINE_SEC" $2
elif [ $1 == 'calib' ]; then
        $CMD
fi

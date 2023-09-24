#!/bin/bash
SPLITS_PY_DIR="/root/nuscenes-devkit/python-sdk/nuscenes/utils"
DATASET="trainval" #"mini"

nusc_link_tables()
{
	TPATH=$(realpath $1)
	pushd ../data/nuscenes/v1.0-$DATASET/v1.0-$DATASET
	for fname in 'sample' 'sample_data' 'instance' 'sample_annotation' 'scene'
	do
		if [[ ! -L "$fname.json" ]]; then
			mv $fname.json $fname.json.backup # backup the original tables
			ln -s "$TPATH/$fname.json"
		fi
	done
	popd
}

nusc_revert_tables()
{
	pushd ../data/nuscenes/v1.0-$DATASET/v1.0-$DATASET
	for fname in 'sample' 'sample_data' 'instance' 'sample_annotation' 'scene'
	do
		if [[ -L "$fname.json" ]]; then
			rm $fname.json
			mv $fname.json.backup $fname.json
		fi
	done
	popd
}

clear_data()
{
	pushd ../data/nuscenes/v1.0-$DATASET
	rm -rf gt_database* *pkl
	popd
}

copy_data()
{
	clear_data
	data_path="./nusc_generated_data/$1/$2"
	echo "Copying from "$data_path
	cp -r $data_path/* ../data/nuscenes/v1.0-$DATASET
}

save_data()
{
	PTH=$(realpath $1)
	pushd ../data/nuscenes/v1.0-$DATASET
	cp -r gt_database* *pkl $PTH
	popd
}

gen_data()
{
	clear_data
	pushd ..
	python -m pcdet.datasets.nuscenes.nuscenes_dataset \
		--func create_nuscenes_infos \
		--cfg_file tools/cfgs/dataset_configs/nuscenes_dataset.yaml \
		--version v1.0-$DATASET
	popd
	sleep 1
}

link_data()
{
	nusc_revert_tables
	nusc_link_tables "nusc_tables_and_dicts/$1/tables"
	for f in token_to_anns.json token_to_pos.json
	do
		rm -f $f
		ln -s "nusc_tables_and_dicts/$1/$f"
	done
	link_infos $1
}


link_infos()
{
	clear_data
	data_path="./nusc_tables_and_dicts/$1/generated_data"
	for pth in $data_path/*
	do
		pth=$(realpath $pth)
		pushd ../data/nuscenes/v1.0-$DATASET
		ln -s $pth
		popd
	done
}


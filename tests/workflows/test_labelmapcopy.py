import os
import copy
import tempfile
import textwrap
from io import StringIO

import pytest
from requests import HTTPError
from ruamel.yaml import YAML
yaml = YAML()

import numpy as np
import pandas as pd

from neuclease.util import round_box, overwrite_subvol
from neuclease.dvid import create_labelmap_instance, post_labelmap_voxels, fetch_labelmap_voxels, post_labelmap_blocks

from flyemflows.util import downsample
from flyemflows.bin.launchflow import launch_flow

# Overridden below when running from __main__
CLUSTER_TYPE = os.environ.get('CLUSTER_TYPE', 'local-cluster')


@pytest.fixture
def setup_dvid_segmentation_input(setup_dvid_repo, random_segmentation):
    dvid_address, repo_uuid = setup_dvid_repo
 
    input_segmentation_name = 'labelmapcopy-segmentation-input'
    output_segmentation_name = 'labelmapcopy-segmentation-output'

    partial_output_segmentation_name = 'labelmapcopy-segmentation-partial-output'
 
    max_scale = 3
    already_exists = False

    try:
        create_labelmap_instance(dvid_address, repo_uuid, input_segmentation_name, max_scale=max_scale)
        create_labelmap_instance(dvid_address, repo_uuid, partial_output_segmentation_name, max_scale=max_scale)
    except HTTPError as ex:
        if ex.response is not None and 'already exists' in ex.response.content.decode('utf-8'):
            already_exists = True

    expected_vols = {}
    for scale in range(1+max_scale):
        if scale == 0:
            scaled_vol = random_segmentation
        else:
            scaled_vol = downsample(scaled_vol, 2, 'labels-numba')
        expected_vols[scale] = scaled_vol
        
        if not already_exists:
            scaled_box = round_box([(0,0,0), scaled_vol.shape], 64, 'out')
            aligned_vol = np.zeros(scaled_box[1], np.uint64)
            overwrite_subvol(aligned_vol, [(0,0,0), scaled_vol.shape], scaled_vol)
            post_labelmap_voxels(dvid_address, repo_uuid, input_segmentation_name, (0,0,0), aligned_vol, scale=scale)


    if not already_exists:
        # Create a 'partial' output volume that is the same (bitwise) as the input except for some blocks.
        scaled_box = np.array([(0,0,0), random_segmentation.shape])
        scaled_box[1,-1] = 192
        for scale in range(1+max_scale):
            scaled_box = round_box(scaled_box // (2**scale), 64, 'out')
            raw_blocks = fetch_labelmap_voxels(dvid_address, repo_uuid, input_segmentation_name, scaled_box, scale, supervoxels=True, format='raw-response')
            post_labelmap_blocks(dvid_address, repo_uuid, partial_output_segmentation_name, [(0,0,0)], raw_blocks, scale, is_raw=True)
    
        block = np.random.randint(1_000_000, 1_000_010, size=(64,64,64), dtype=np.uint64)
        post_labelmap_voxels(dvid_address, repo_uuid, partial_output_segmentation_name, (0,128,64), block, 0, downres=True)

    partial_vol = fetch_labelmap_voxels(dvid_address, repo_uuid, partial_output_segmentation_name, [(0,0,0), random_segmentation.shape], supervoxels=True)
    
    template_dir = tempfile.mkdtemp(suffix="labelmapcopy-template")
 
    config_text = textwrap.dedent(f"""\
        workflow-name: labelmapcopy
        cluster-type: {CLUSTER_TYPE}
         
        input:
          dvid:
            server: {dvid_address}
            uuid: {repo_uuid}
            segmentation-name: {input_segmentation_name}
            supervoxels: true
           
          geometry:
            message-block-shape: [512,64,64]
            available-scales: [0,1,2,3]
 
        output:
          dvid:
            server: {dvid_address}
            uuid: {repo_uuid}
            segmentation-name: {output_segmentation_name}
            supervoxels: true
            disable-indexing: true
            create-if-necessary: true
        
        labelmapcopy:
          slab-shape: [512,128,64]
          dont-overwrite-identical-blocks: true
    """)
 
    with open(f"{template_dir}/workflow.yaml", 'w') as f:
        f.write(config_text)
 
    yaml = YAML()
    with StringIO(config_text) as f:
        config = yaml.load(f)
 
    return template_dir, config, expected_vols, partial_vol, dvid_address, repo_uuid, output_segmentation_name, partial_output_segmentation_name


def test_labelmapcopy(setup_dvid_segmentation_input, disable_auto_retry):
    template_dir, _config, expected_vols, partial_vol, dvid_address, repo_uuid, output_segmentation_name, _partial_output_segmentation_name = setup_dvid_segmentation_input

    execution_dir, workflow = launch_flow(template_dir, 1)
    final_config = workflow.config

    output_box_xyz = np.array( final_config['output']['geometry']['bounding-box'] )
    output_box_zyx = output_box_xyz[:,::-1]
    
    max_scale = final_config['labelmapcopy']['max-scale']
    for scale in range(1+max_scale):
        scaled_box = output_box_zyx // (2**scale)
        output_vol = fetch_labelmap_voxels(dvid_address, repo_uuid, output_segmentation_name, scaled_box, scale=scale)
        assert (output_vol == expected_vols[scale]).all(), \
            f"Written vol does not match expected for scale {scale}"

    svs = pd.read_csv(f'{execution_dir}/recorded-labels.csv')['sv']
    assert set(svs) == set(np.unique(expected_vols[0].reshape(-1)))


def test_labelmapcopy_partial(setup_dvid_segmentation_input, disable_auto_retry):
    template_dir, config, expected_vols, partial_vol, dvid_address, repo_uuid, _output_segmentation_name, partial_output_segmentation_name = setup_dvid_segmentation_input
    
    config = copy.deepcopy(config)
    config["output"]["dvid"]["segmentation-name"] = partial_output_segmentation_name
    
    yaml = YAML()
    yaml.default_flow_style = False
    with open(f"{template_dir}/workflow.yaml", 'w') as f:
        yaml.dump(config, f)
    
    execution_dir, workflow = launch_flow(template_dir, 1)
    final_config = workflow.config

    output_box_xyz = np.array( final_config['output']['geometry']['bounding-box'] )
    output_box_zyx = output_box_xyz[:,::-1]
    
    max_scale = final_config['labelmapcopy']['max-scale']
    for scale in range(1+max_scale):
        scaled_box = output_box_zyx // (2**scale)
        output_vol = fetch_labelmap_voxels(dvid_address, repo_uuid, partial_output_segmentation_name, scaled_box, scale=scale)
        assert (output_vol == expected_vols[scale]).all(), \
            f"Written vol does not match expected for scale {scale}"

    # Any labels NOT in the partial vol had to be written.
    written_labels = pd.unique(expected_vols[0][expected_vols[0] != partial_vol])
    assert len(written_labels) > 0, \
        "This test data was chosen poorly -- there's no difference between the partial and full labels!"

    svs = pd.read_csv(f'{execution_dir}/recorded-labels.csv')['sv']
    assert set(svs) == set(written_labels)


if __name__ == "__main__":
    if 'CLUSTER_TYPE' in os.environ:
        import warnings
        warnings.warn("Disregarding CLUSTER_TYPE when running via __main__")
    
    #from neuclease import configure_default_logging
    #configure_default_logging()
    
    CLUSTER_TYPE = os.environ['CLUSTER_TYPE'] = "synchronous"
    args = ['-s', '--tb=native', '--pyargs', 'tests.workflows.test_labelmapcopy']
    #args += ['-k', 'labelmapcopy_partial']
    pytest.main(args)

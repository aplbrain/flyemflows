import os
import tempfile
import textwrap
from io import StringIO

import h5py
import numpy as np

from dvidutils import downsample_labels
from neuclease.util import box_to_slicing
from neuclease.dvid import create_voxel_instance, post_raw, fetch_raw

import pytest
from ruamel.yaml import YAML
from flyemflows.bin.launchworkflow import launch_workflow

TESTVOL_SHAPE = (256,256,256)

# Overridden below when running from __main__
CLUSTER_TYPE = os.environ.get('CLUSTER_TYPE', 'local-cluster')
#CLUSTER_TYPE = os.environ.get('CLUSTER_TYPE', 'synchronous')

@pytest.fixture
def setup_dvid_grayscale_input(setup_dvid_repo):
    dvid_address, repo_uuid = setup_dvid_repo
 
    input_grayscale_name = 'grayscale-input'
    output_grayscale_name = 'grayscale-output-from-dvid'
 
    create_voxel_instance(dvid_address, repo_uuid, input_grayscale_name, 'uint8blk')
     
    # Create volume and post to dvid
    volume = np.random.randint(255, size=TESTVOL_SHAPE, dtype=np.uint8)
    post_raw(dvid_address, repo_uuid, input_grayscale_name, (0,0,0), volume)
     
    template_dir = tempfile.mkdtemp(suffix="copygrayscale-template")
 
    config_text = textwrap.dedent(f"""\
        workflow-name: copygrayscale
        cluster-type: {CLUSTER_TYPE}
         
        input:
          dvid:
            server: {dvid_address}
            uuid: {repo_uuid}
            grayscale-name: {input_grayscale_name}
           
          geometry:
            message-block-shape: [64,64,512]
            bounding-box: [[0,0,100], [256,200,256]]
 
        output:
          dvid:
            server: {dvid_address}
            uuid: {repo_uuid}
            grayscale-name: {output_grayscale_name}
            compression: raw
           
          geometry: {{}} # Auto-set from input
 
        copygrayscale:
          max-pyramid-scale: 1
          slab-depth: 128
    """)
 
    with open(f"{template_dir}/workflow.yaml", 'w') as f:
        f.write(config_text)
 
    yaml = YAML()
    with StringIO(config_text) as f:
        config = yaml.load(f)
 
    return template_dir, config, volume, dvid_address, repo_uuid, output_grayscale_name


@pytest.fixture
def setup_hdf5_grayscale_input(setup_dvid_repo):
    dvid_address, repo_uuid = setup_dvid_repo
    template_dir = tempfile.mkdtemp(suffix="copygrayscale-template")
    
    # Create volume, write to HDF5
    volume = np.random.randint(10, size=TESTVOL_SHAPE, dtype=np.uint8)
    volume_path = f"{template_dir}/volume.h5"
    with h5py.File(volume_path, 'w') as f:
        f['volume'] = volume
    
    
    output_grayscale_name = 'grayscale-output-from-hdf5'
    
    config_text = textwrap.dedent(f"""\
        workflow-name: copygrayscale
        cluster-type: {CLUSTER_TYPE}
        
        input:
          hdf5:
            path: {volume_path}
            dataset: volume
          
          geometry:
            message-block-shape: [64,64,256]
            bounding-box: [[0,0,100], [256,200,256]]

          # Enable multi-scale, since otherwise
          # Hdf5VolumeService doesn't support it out-of-the box
          rescale-level: 0

        output:
          dvid:
            server: {dvid_address}
            uuid: {repo_uuid}
            grayscale-name: {output_grayscale_name}
            compression: raw
          
          geometry: {{}} # Auto-set from input
        
        copygrayscale:
          max-pyramid-scale: 1
          slab-depth: 128
    """)

    with open(f"{template_dir}/workflow.yaml", 'w') as f:
        f.write(config_text)

    yaml = YAML()
    with StringIO(config_text) as f:
        config = yaml.load(f)

    return template_dir, config, volume, dvid_address, repo_uuid, output_grayscale_name


def _run(setup, check_scale_0=True):
    template_dir, config, volume, dvid_address, repo_uuid, output_grayscale_name = setup

    yaml = YAML()
    yaml.default_flow_style = False
    
    # re-dump config in case it's been changed by a specific test
    with open(f"{template_dir}/workflow.yaml", 'w') as f:
        yaml.dump(config, f)
    
    _execution_dir, workflow = launch_workflow(template_dir, 1)
    final_config = workflow.config

    box_xyz = np.array( final_config['input']['geometry']['bounding-box'] )
    box_zyx = box_xyz[:,::-1]
    
    output_vol = fetch_raw(dvid_address, repo_uuid, output_grayscale_name, box_zyx)
    expected_vol = volume[box_to_slicing(*box_zyx)]
    
    if check_scale_0:
        assert (output_vol == expected_vol).all(), \
            "Written vol does not match expected"
    
    return box_zyx, expected_vol


def test_copygrayscale_from_dvid_to_dvid(setup_dvid_grayscale_input):
    _box_zyx, _expected_vol = _run(setup_dvid_grayscale_input)
   
   
def test_copygrayscale_from_hdf5_to_dvid(setup_hdf5_grayscale_input):
    _box_zyx, _expected_vol = _run(setup_hdf5_grayscale_input)
 
 
def test_copygrayscale_from_hdf5_to_dvid_multiscale(setup_hdf5_grayscale_input):
    _template_dir, config, _volume, dvid_address, repo_uuid, output_grayscale_name = setup_hdf5_grayscale_input
     
    # Modify the config from above to compute pyramid scales,
    # and choose a bounding box that is aligned with the bricks even at scale 2
    # (just for easier testing).
    config["input"]["geometry"]["bounding-box"] = [[0,0,0],[128,256,128]]
    config["copygrayscale"]["max-pyramid-scale"] = 2
    config["copygrayscale"]["pyramid-source"] = "compute-as-labels" # This test is easier to write if we use this downsampling method
 
    box_zyx, scale_0_vol = _run( setup_hdf5_grayscale_input )
 
    scale_1_vol = downsample_labels(scale_0_vol, 2, True)
    scale_2_vol = downsample_labels(scale_1_vol, 2, True)
 
    # Check the other scales -- be careful to extract exactly one brick.
    output_vol = fetch_raw(dvid_address, repo_uuid, output_grayscale_name + '_1', box_zyx // 2)
    assert (output_vol == scale_1_vol).all(), \
        "Scale 1: Written vol does not match expected"
 
    # Check the other scales -- be careful to extract exactly one brick.
    output_vol = fetch_raw(dvid_address, repo_uuid, output_grayscale_name + '_2', box_zyx // 4)
    assert (output_vol == scale_2_vol).all(), \
        "Scale 2: Written vol does not match expected"


def test_copygrayscale_from_hdf5_to_dvid_with_constrast_adjustment(setup_hdf5_grayscale_input):
    """
    Use the "constrast-adjustment" setting.
    This test doesn't actually check the output, but at least it exercises the code enough to catch basic issues.
    """
    _template_dir, config, _volume, _dvid_address, _repo_uuid, _output_grayscale_name = setup_hdf5_grayscale_input

    config["input"]["geometry"]["bounding-box"] = [[0,0,0],[256,128,128]]
    config["copygrayscale"]["contrast-adjustment"] = "hotknife-destripe"
    config["copygrayscale"]["hotknife-seams"] = [-1,50,100,150,256] # Note: These are X-coordinates.

    _box_zyx, _scale_0_vol = _run( setup_hdf5_grayscale_input, check_scale_0=False )
    

if __name__ == "__main__":
    if 'CLUSTER_TYPE' in os.environ:
        import warnings
        warnings.warn("Disregarding CLUSTER_TYPE when running via __main__")
    
    CLUSTER_TYPE = os.environ['CLUSTER_TYPE'] = "synchronous"
    pytest.main(['-s', '--tb=native', '--pyargs', 'tests.workflows.test_copygrayscale'])

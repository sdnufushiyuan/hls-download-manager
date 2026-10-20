"""Build a static exclusion mask from consecutive valid UTC-date water QA.

Inputs must be unfiltered original Fmask daily rasters, not clear-land exports.
No network access or guessed water labels; clouds/missing and sensor conflicts
are skipped, valid land resets the streak. The mask applies to all target dates.
"""
import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import uuid
import hashlib

import numpy as np
import rasterio
from rasterio.windows import Window
from download_hls_optram import file_hash
from hls_daily_mosaic import atomic_json,no_symlink

POLICY='consecutive-valid-date-water-all-dates-v1'


def step(streak,ever,water,land,valid):
    """One UTC date; conflicts have valid=False and do not add an observation."""
    streak[valid&land]=0
    streak[valid&water]=np.minimum(streak[valid&water]+1,2)
    ever|=streak>=2


def build(manifest,output):
    if manifest.get('raw_unfiltered_fmask') is not True:
        raise ValueError('Original unfiltered Fmask required; NoData is not water evidence')
    records=manifest['observations']
    if not isinstance(records,list) or not records:raise ValueError('No QA observations')
    groups=defaultdict(list);grid=None;fingerprints=[];seen=set()
    for row in records:
        date=row['date'];datetime.strptime(date,'%Y%m%d')
        if len(date)!=8 or row['product'] not in ('L30','S30'):raise ValueError('Invalid date/product')
        identity=(date,row['product'])
        if identity in seen:raise ValueError('Provide one fixed-grid original QA raster per product and UTC day')
        seen.add(identity)
        path=Path(row['path']);no_symlink(path)
        digest=file_hash(path)
        if digest!=row['sha256']:raise ValueError('QA input checksum mismatch')
        with rasterio.open(path) as ds:
            current=(ds.width,ds.height,ds.transform,ds.crs)
            if ds.count!=1 or ds.dtypes[0]!='uint8':raise ValueError('Expected one-band uint8 original Fmask')
            if grid is not None and grid!=current:raise ValueError('All QA rasters must share the exact ROI grid')
            grid=current;profile=ds.profile.copy()
        groups[date].append(path);fingerprints.append({'date':date,'product':row['product'],'sha256':digest})
    output=Path(output);no_symlink(output)
    if output.exists() or output.with_suffix('.json').exists():raise ValueError('Choose new mask output; refusing overwrite')
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=output.with_name(output.name+'.'+uuid.uuid4().hex+'.part')
    expected=[];excluded=0
    try:
        profile.update(dtype='uint8',count=1,nodata=None,compress='deflate',tiled=True,
                       blockxsize=256,blockysize=256,BIGTIFF='IF_SAFER')
        with rasterio.open(temp,'w',**profile) as dst:
            dst.set_band_description(1,'ever_two_consecutive_valid_dates_water')
            dst.update_tags(policy=POLICY,consecutive_valid_dates='2',same_day='consensus; conflicts skipped',
                            meaning='1=exclude this position on every date; 0=no qualifying sequence')
            for r in range(0,grid[1],256):
                for c in range(0,grid[0],256):
                    win=Window(c,r,min(256,grid[0]-c),min(256,grid[1]-r))
                    shape=(int(win.height),int(win.width));streak=np.zeros(shape,dtype='uint8');ever=np.zeros(shape,dtype=bool)
                    for date,paths in sorted(groups.items()):
                        water=np.zeros(shape,dtype=bool);land=water.copy()
                        for path in paths:
                            with rasterio.open(path) as ds:qa=ds.read(1,window=win,masked=True)
                            values=qa.data
                            valid=(~np.ma.getmaskarray(qa))&(values!=255)&((values&30)==0)&((values>>6)==1)
                            water|=valid&((values&32)>0);land|=valid&((values&32)==0)
                        valid=water^land  # Distinct dates; two sensors on the same date count once.
                        step(streak,ever,water,land,valid)
                    a=ever.astype('uint8');dst.write(a,1,window=win)
                    expected.append((win,hashlib.sha256(a.tobytes()).hexdigest()));excluded+=int(ever.sum())
        with rasterio.open(temp) as ds:
            for win,digest in expected:
                if hashlib.sha256(ds.read(1,window=win).tobytes()).hexdigest()!=digest:raise ValueError('Mask readback mismatch')
        for row in records:
            if file_hash(Path(row['path']))!=row['sha256']:raise ValueError('QA source changed during mask build')
        os.replace(temp,output)
        receipt={'policy':POLICY,'consecutive_valid_dates':2,'apply_to':'all dates, all L30/S30/FUSED products',
            'same_day':'one UTC-date vote; clear-sensor disagreement skipped',
            'QA':'bits1-4 clear; aerosol=low; bit5 water; cloud/missing skipped; land resets',
            'raw_unfiltered_fmask':True,'inputs':fingerprints,'unique_UTC_dates':len(groups),
            'excluded_pixels':excluded,'sha256':file_hash(output),'path':str(output)}
        atomic_json(output.with_suffix('.json'),receipt);return receipt
    finally:temp.unlink(missing_ok=True)


def apply_mask(input_path,mask_path,output_path):
    """Filter one main/source/time raster with the same static geographic mask."""
    input_path,mask_path,output_path=map(Path,(input_path,mask_path,output_path))
    for path in (input_path,mask_path,output_path):no_symlink(path)
    receipt=json.loads(mask_path.with_suffix('.json').read_text(encoding='utf-8'))
    if receipt.get('policy')!=POLICY or receipt.get('sha256')!=file_hash(mask_path):raise ValueError('Unverified water mask')
    if output_path.exists() or output_path.with_suffix('.json').exists():raise ValueError('Refusing overwrite')
    original_sha=file_hash(input_path)
    output_path.parent.mkdir(parents=True,exist_ok=True)
    temp=output_path.with_name(output_path.name+'.'+uuid.uuid4().hex+'.part');expected=[]
    try:
        with rasterio.open(input_path) as src,rasterio.open(mask_path) as mask:
            if ((src.width,src.height,src.transform,src.crs)!=(mask.width,mask.height,mask.transform,mask.crs)
                    or src.nodata is None):raise ValueError('Exact aligned grid and source NoData required')
            with rasterio.open(temp,'w',**src.profile) as dst:
                dst.descriptions=src.descriptions;dst.update_tags(**src.tags(),water_history_policy=POLICY,
                    water_history_sha256=receipt['sha256'])
                for _,win in src.block_windows(1):
                    a=src.read(window=win);excluded=mask.read(1,window=win)
                    if not np.all((excluded==0)|(excluded==1)):raise ValueError('Invalid exclusion code')
                    a[:,excluded==1]=src.nodata;dst.write(a,window=win)
                    expected.append((win,hashlib.sha256(a.tobytes()).hexdigest()))
        with rasterio.open(temp) as ds:
            for win,digest in expected:
                if hashlib.sha256(ds.read(window=win).tobytes()).hexdigest()!=digest:raise ValueError('Filtered readback mismatch')
        if file_hash(input_path)!=original_sha or file_hash(mask_path)!=receipt['sha256']:raise ValueError('Input changed')
        os.replace(temp,output_path)
        saved={'policy':POLICY,'source_sha256':original_sha,'water_mask_sha256':receipt['sha256'],
            'sha256':file_hash(output_path),'path':str(output_path),'source':str(input_path)}
        atomic_json(output_path.with_suffix('.json'),saved);return saved
    finally:temp.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    make=sub.add_parser('build');make.add_argument('--manifest',type=Path,required=True);make.add_argument('--output',type=Path,required=True)
    use=sub.add_parser('apply');use.add_argument('--input',type=Path,required=True);use.add_argument('--mask',type=Path,required=True);use.add_argument('--output',type=Path,required=True)
    for p in (make,use):p.add_argument('--run',action='store_true')
    args=parser.parse_args(argv)
    if not args.run:print('Plan only; no files written');return 0
    result=build(json.loads(args.manifest.read_text(encoding='utf-8')),args.output) if args.command=='build' else apply_mask(args.input,args.mask,args.output)
    print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())

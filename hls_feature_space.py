"""Offline sample model-domain QC, explicit reviewed exclusions and NDVI/STR plots.

This is a downstream analysis tool; it never changes archives or infers that
high STR, a dark neighbourhood or historical water alone proves a bad pixel.
"""
import argparse
import json
from pathlib import Path
import uuid
import os
import re

import numpy as np
import rasterio
from download_hls_optram import BANDS, NODATA, file_hash, hash_object
from hls_node_optimizer import atomic_json
from hls_daily_mosaic import no_symlink

POLICY='sample-model-domain-explicit-review-v1'


def model_mask(a):
    valid=np.all(np.isfinite(a)&(a!=NODATA),axis=0)
    return (valid & np.all((a[:3]>=0)&(a[:3]<=1),axis=0) & (a[2]>0)
            & ((a[0]+a[1])>1e-6) & (np.abs(a[3])<=1) & (a[4]>=0))


def process(l30, s30, output, region, date, review=None, plot=False):
    if not region.isascii() or not region.isalnum():raise ValueError('Use an alphanumeric region code')
    from datetime import datetime
    if not re.fullmatch(r'\d{8}',date):raise ValueError('Date must be YYYYMMDD')
    datetime.strptime(date,'%Y%m%d')
    paths={'L30':Path(l30),'S30':Path(s30)}
    arrays={};inputs={};grids=[]
    for p,path in paths.items():
        no_symlink(path)
        inputs[p]={'sha256':file_hash(path)}
        with rasterio.open(path) as ds:
            if ds.descriptions!=BANDS or ds.nodata!=NODATA:raise ValueError('Expected six-band HLS daily TIFF with NoData=-9999')
            arrays[p]=ds.read();profile=ds.profile.copy()
            grids.append((ds.width,ds.height,ds.transform,ds.crs))
        a=arrays[p];v=np.all(np.isfinite(a)&(a!=NODATA),axis=0)
        if np.any(np.any(a==NODATA,axis=0)&np.any(a!=NODATA,axis=0)) or not np.all(np.isfinite(a)):
            raise ValueError('Inconsistent mask or nonfinite input')
        qa=a[5,v]
        if not np.all((qa==qa.astype('uint8'))&((qa.astype('uint8')&62)==0)&((qa.astype('uint8')>>6)==1)):
            raise ValueError('Input must retain strict clear-land, low-aerosol QA')
        r,n,s=(a[i,v].astype('float64') for i in range(3))
        if np.any(s<=0) or np.any(np.abs(n+r)<=1e-6):raise ValueError('Invalid index denominator')
        if not (np.allclose(a[3,v],(n-r)/(n+r),rtol=2e-6,atol=1e-7) and
                np.allclose(a[4,v],(1-s)**2/(2*s),rtol=2e-6,atol=1e-7)):
            raise ValueError('Stored indices disagree with reflectances')
    if len(set(grids))!=1:raise ValueError('Inputs must have the same CRS, transform and shape; no implicit resampling')
    no_symlink(Path(output))
    output=Path(output).resolve()
    if any(output==p.resolve() or output in p.resolve().parents or p.resolve().parent in output.parents for p in paths.values()):
        raise ValueError('Keep analysis output separate from input directories')
    contract={'policy':POLICY,'region':region,'date_utc':date,'inputs':inputs,'review':review,'plot':bool(plot)}
    masks={p:model_mask(a) for p,a in arrays.items()}
    seen=set()
    if review is not None:
        if review.get('input_sha256')!={p:i['sha256'] for p,i in inputs.items()}:
            raise ValueError('Review must bind both exact input SHA256 values')
        for row in review.get('reject',[]):
            p=row.get('product');r=row.get('row');c=row.get('col')
            if (p not in masks or type(r) is not int or type(c) is not int or
                    not 0<=r<grids[0][1] or not 0<=c<grids[0][0] or
                    not isinstance(row.get('reason'),str) or not row['reason'].strip() or
                    not isinstance(row.get('evidence'),str) or not row['evidence'].strip()):
                raise ValueError('Each rejection requires product, in-grid row/col, reason and evidence')
            if (p,r,c) in seen or not masks[p][r,c]:raise ValueError('Duplicate or already invalid reviewed observation')
            seen.add((p,r,c));masks[p][r,c]=False
    fingerprint=hash_object(contract)
    receipt=output/'analysis.json'
    if output.exists():
        if not receipt.is_file():raise ValueError('Existing output has no committed receipt')
        saved=json.loads(receipt.read_text())
        if saved.get('fingerprint')!=fingerprint:raise ValueError('Different analysis already exists; choose a new output directory')
        for rel,digest in saved['files'].items():
            if Path(rel).is_absolute() or '..' in Path(rel).parts:raise ValueError('Invalid receipt file path')
            no_symlink(output/rel)
            if file_hash(output/rel)!=digest:raise ValueError('Analysis output checksum mismatch')
        return saved
    clean={p:np.where(masks[p][None],a,NODATA).astype('float32') for p,a in arrays.items()}
    source=np.where(masks['L30'],1,np.where(masks['S30'],2,0)).astype('uint8')
    clean['FUSED']=np.where(masks['L30'][None],clean['L30'],clean['S30'])
    masks['FUSED']=source>0
    output.mkdir(parents=True)
    files={}
    def write(path,a,nodata,labels):
        if a.ndim==2:a=a[None]
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.part')
        prof=profile.copy();prof.update(count=len(a),dtype=str(a.dtype),nodata=nodata,compress='deflate',
            tiled=True,blockxsize=256,blockysize=256,predictor=3 if a.dtype.kind=='f' else 2,BIGTIFF='IF_SAFER')
        try:
            with rasterio.open(temporary,'w',**prof) as ds:
                ds.write(a);ds.descriptions=tuple(labels);ds.update_tags(qc_policy=POLICY,fingerprint=fingerprint)
            with rasterio.open(temporary) as ds:
                if not np.array_equal(ds.read(),a):raise ValueError('Written pixels differ')
            os.replace(temporary,path);files[str(path.relative_to(output))]=file_hash(path)
        finally:temporary.unlink(missing_ok=True)
    stats={}
    for p,a in clean.items():
        write(output/p/region/date[:4]/f'{region}_{p}_{date}_QC.tif',a,NODATA,BANDS)
        v=masks[p];stats[p]={'valid_pixels':int(v.sum()),'STR_gt_15_retained':int(np.sum(v&(a[4]>15)))}
    write(output/'FUSED'/region/date[:4]/f'{region}_FUSED_{date}_QC_source.tif',source,0,('source_0_none_1_L30_2_S30',))
    if plot:
        plot_comparison(clean,masks,output,region,date)
        for ext in ('png','pdf'):files['NDVI_STR_comparison.'+ext]=file_hash(output/('NDVI_STR_comparison.'+ext))
    for p,path in paths.items():
        if file_hash(path)!=inputs[p]['sha256']:raise ValueError('Input changed during analysis')
    saved={'fingerprint':fingerprint,'contract':contract,'datasets':stats,'files':files,
        'source_pixels':{'L30':int(np.sum(source==1)),'S30':int(np.sum(source==2))},
        'time_provenance':'No new per-pixel time inferred from a daily TIFF; retain original daily receipts/time maps separately'}
    atomic_json(receipt,saved)
    return saved


def plot_comparison(clean,masks,output,region,date):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm
    products=('L30','S30','FUSED');x=np.linspace(-1,1,181)
    pts={p:(clean[p][3,masks[p]],clean[p][4,masks[p]]) for p in products}
    nonempty=[y for xx,y in pts.values() if len(y)]
    max_y=max((float(y.max()) for y in nonempty),default=15)
    positive=np.concatenate([y[y>0] for y in nonempty]) if nonempty else np.array([])
    low=min(float(positive.min())*.8,1) if len(positive) else .001
    fig,axes=plt.subplots(2,3,figsize=(14,8.5),layout='constrained')
    for row,edges in enumerate((np.linspace(0,15,151),np.concatenate(([0],np.geomspace(low,max(max_y*1.1,low*10),181))))):
        hh={p:np.histogram2d(*pts[p],bins=(x,edges))[0] for p in products}
        norm=LogNorm(1,max(2,max(float(h.max()) for h in hh.values())))
        for i,p in enumerate(products):
            ax=axes[row,i];mesh=ax.pcolormesh(x,edges,np.ma.masked_equal(hh[p].T,0),cmap='viridis',norm=norm,rasterized=True)
            ax.set(xlabel='NDVI',ylabel='STR',title=f"{p} | n={len(pts[p][0]):,}",xlim=(-1,1),ylim=(edges[0],edges[-1]))
            if row:
                ax.set_yscale('symlog',linthresh=low);ax.axhline(15,ls='--',color='gray',lw=.7)
                if int(hh[p].sum())!=len(pts[p][0]):raise ValueError('Full-range plot lost pixels')
        fig.colorbar(mesh,ax=axes[row,:],label='Pixel count (log colour scale)')
    fig.suptitle(f'{region} {date} | NDVI-STR model-domain QC; top: core view, bottom: full range')
    for ext in ('png','pdf'):fig.savefig(output/('NDVI_STR_comparison.'+ext),dpi=180)
    plt.close(fig)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--l30',required=True,type=Path);parser.add_argument('--s30',required=True,type=Path)
    parser.add_argument('--output-root',required=True,type=Path);parser.add_argument('--region',required=True)
    parser.add_argument('--date',required=True,help='UTC YYYYMMDD')
    parser.add_argument('--review',type=Path,help='Explicit reviewed exclusions bound to both input SHA256 values')
    parser.add_argument('--plot',action='store_true');parser.add_argument('--run',action='store_true')
    args=parser.parse_args(argv)
    if not args.run:
        print(json.dumps({'mode':'plan','output_root':str(args.output_root),'network':False}));return 0
    review=json.loads(args.review.read_text(encoding='utf-8')) if args.review else None
    saved=process(args.l30,args.s30,args.output_root,args.region,args.date,review,args.plot)
    print(json.dumps(saved['datasets']));return 0


if __name__=='__main__':raise SystemExit(main())

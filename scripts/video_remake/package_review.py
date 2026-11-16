"""Bundle only reviewed edit inputs; never include env, databases or other captures."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import zipfile
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / 'submission/video-work/remake-20261116'
OUT = ROOT / 'output/video-remake-20261116'
VIDEO = ROOT / 'submission/演示视频-新版-20261116.mp4'


def sha(path):
    return hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT/'scripts/video_remake/edit.json').read_text(encoding='utf8'))
    build = json.loads(VIDEO.with_suffix('.build.json').read_text(encoding='utf8'))
    if sha(VIDEO) != build['sha256'] or not build['full_decode_passed']:
        raise ValueError('Final video does not match its successful decode record')
    inputs = {ROOT / s['media'] for s in config['segments'] if s.get('media')}
    inputs.update((WORK/'audio').glob('*'))
    for name in ('board-add-live','board-filter-live','board-refresh-live','graph-replay'):
        inputs.update((WORK/'captures'/name).glob('*'))
    inputs.update(WORK/n for n in ('preview-receipt.json','ui-recording-notes.json'))
    inputs.add(WORK/'captures/board-actions-edit/edit-note.txt')
    inputs.add(WORK/'rejected-draft-01/review.json')
    inputs.add(WORK/'rejected-draft-02/review.json')
    inputs = sorted(p for p in inputs if p.is_file())
    for path in inputs:
        path.resolve().relative_to(WORK.resolve())
    archive = ROOT/'submission/video-remake-20261116-source.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for path in inputs:
            z.write(path, path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('Source archive CRC validation failed')
    for suffix, name in (('.build.json','build.json'),('.contact.jpg','contact.jpg'),('.srt','subtitles.srt')):
        shutil.copy2(VIDEO.with_suffix(suffix),OUT/name)
    shutil.copy2(WORK/'preview-receipt.json',OUT/'preview-receipt.json')
    shutil.copy2(WORK/'rejected-draft-01/review.json',OUT/'rejected-draft-01.json')
    shutil.copy2(WORK/'rejected-draft-02/review.json',OUT/'rejected-draft-02.json')
    f = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([f,'-hide_banner','-loglevel','error','-y','-ss','13','-i',str(VIDEO),'-frames:v','1',str(OUT/'poster.png')],check=True)
    audio = subprocess.run([f,'-hide_banner','-i',str(VIDEO),'-af','volumedetect','-vn','-sn','-dn','-f','null','-'],capture_output=True,encoding='utf8',errors='replace',check=True)
    (OUT/'audio-qa.txt').write_text(audio.stderr,encoding='utf8')
    receipt = {'video':VIDEO.relative_to(ROOT).as_posix(),'video_sha256':sha(VIDEO),'video_bytes':VIDEO.stat().st_size,
        'duration_seconds':build['duration_probed'],'width':1920,'height':1080,'fps':30,'voice':'zh-CN-XiaoxiaoNeural','rate':'-5%',
        'original_video_sha256':build['original_sha256'],'original_unchanged':build['original_unchanged'],
        'full_decode_passed':build['full_decode_passed'],'new_model_calls':0,'live_preview_http_requests':40,
        'source_archive':archive.relative_to(ROOT).as_posix(),'source_archive_sha256':sha(archive),
        'source_archive_bytes':archive.stat().st_size,'source_archive_crc_passed':True,
        'source_input_count':len(inputs), 'source_inputs':[{'path':p.relative_to(ROOT).as_posix(),'sha256':sha(p)} for p in inputs]}
    (OUT/'delivery.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (OUT/'SHA256SUMS.txt').write_text(f"{receipt['video_sha256']}  {VIDEO.name}\n{receipt['source_archive_sha256']}  {archive.name}\n",encoding='utf8')
    print(json.dumps({k:v for k,v in receipt.items() if k!='source_inputs'},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

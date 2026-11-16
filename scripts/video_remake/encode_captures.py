"""Convert CUA CDP screencast frames to clips, preserving observed timestamps.

Run in the isolated media environment. A missing capture.json denotes an
interrupted capture and is never promoted to a successful recording.
"""
from pathlib import Path
import argparse
import json
import subprocess
import imageio_ffmpeg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='submission/video-work/remake-20261116/captures')
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    index = []
    for meta in sorted(root.glob('*/capture.json')):
        data = json.loads(meta.read_text(encoding='utf-8'))
        frames = data['frames']
        if not frames or data.get('errors'):
            index.append({'name': data['name'], 'status': 'capture_failed'})
            continue
        lines = []
        for i, frame in enumerate(frames):
            lines.append("file '" + frame['file'] + "'")
            dt = frames[i + 1]['timestamp'] - frame['timestamp'] if i + 1 < len(frames) else max(.1, data['ended']/1000 - frame['timestamp'])
            lines.append(f'duration {max(1/60, dt):.6f}')
        lines.append("file '" + frames[-1]['file'] + "'")
        manifest = meta.parent / 'frames.ffconcat'
        manifest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        output = meta.parent / (data['name'] + '.mp4')
        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(manifest), '-vf', 'fps=30,format=yuv420p', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-an', '-movflags', '+faststart', str(output)]
        subprocess.run(cmd, check=True, encoding='utf-8', errors='replace')
        index.append({'name':data['name'], 'frames':len(frames), 'duration':round(data['ended']/1000 - frames[0]['timestamp'],3), 'status':'encoded', 'source':data.get('source','CUA CDP Page.screencastFrame'), 'output':str(output.relative_to(root))})
        print(data['name'], len(frames), flush=True)
    (root / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
    # The published demo shortens idle waits, preserving the operation order.
    first, second = root/'board-add-live/board-add-live.mp4', root/'board-filter-live/board-filter-live.mp4'
    if first.is_file() and second.is_file():
        edit_dir = root/'board-actions-edit'
        edit_dir.mkdir(exist_ok=True)
        subprocess.run([ffmpeg,'-y','-hide_banner','-loglevel','error','-i',str(first),'-i',str(second),
            '-filter_complex','[0:v]trim=0:3.8,setpts=PTS-STARTPTS[a];[1:v]trim=0:3.8,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]',
            '-map','[v]','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-an',str(edit_dir/'board-actions-edit.mp4')],check=True)
        (edit_dir/'edit-note.txt').write_text('First 3.8 seconds of add, then first 3.8 seconds of completion/filter. Original order, original speed; idle waits shortened. No generated UI.',encoding='utf-8')


if __name__ == '__main__':
    main()

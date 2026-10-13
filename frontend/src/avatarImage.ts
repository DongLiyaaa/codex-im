export const AVATAR_SIDE = 256;
export const AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const MAX_SOURCE_BYTES = 10 * 1024 * 1024;

/** Center-crops the picture to a square and re-encodes it as a small JPEG data URL the API accepts. */
export async function avatarFromFile(file: File): Promise<string> {
  if (!AVATAR_TYPES.includes(file.type)) throw new Error('请选择 PNG、JPG 或 WebP 图片。');
  if (file.size > MAX_SOURCE_BYTES) throw new Error('图片不能超过 10MB。');
  const url = URL.createObjectURL(file);
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error('无法读取这张图片，请换一张。'));
      el.src = url;
    });
    const side = Math.min(image.naturalWidth, image.naturalHeight);
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = AVATAR_SIDE;
    const context = canvas.getContext('2d');
    if (!side || !context) throw new Error('无法处理这张图片，请换一张。');
    context.fillStyle = '#fff';
    context.fillRect(0, 0, AVATAR_SIDE, AVATAR_SIDE);
    context.drawImage(image, (image.naturalWidth - side) / 2, (image.naturalHeight - side) / 2, side, side, 0, 0, AVATAR_SIDE, AVATAR_SIDE);
    return canvas.toDataURL('image/jpeg', 0.88);
  } finally { URL.revokeObjectURL(url); }
}
